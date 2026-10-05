"""Durable run event log and the SSE sink built on it.

The rules this exists to satisfy (AGENTS.md, Streaming Rules):

- **Monotonic event ids.** ``id`` is the line number in the run's JSONL file, so it
  is monotonic by construction and survives a process restart.
- **Persist before delivering.** ``emit`` writes the event, then queues it. An
  acknowledged event is therefore already on disk, and a client that disconnects
  immediately after receiving it can still replay it. The reverse order loses
  events exactly when they are hardest to notice.
- **No in-memory-only state.** The queue is a convenience for the live consumer;
  the file is the truth. There is no compaction routine in this module, and that is
  deliberate: a routine whose job is to trim a run's events is one bad interaction
  away from removing the entries a suspended run needs to resume, and a run in this
  system can stay suspended for days.
- **Bounded inline payloads.** A single event larger than the cap is offloaded to a
  sidecar file and the event carries the reference, so a replay does not have to
  read a multi-megabyte body into memory to learn that an answer was long.

Event ids are per run, not global. A client reconnects to one run, and a global
counter would make the reconnect cursor depend on unrelated traffic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ragos.core.config import get_settings
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.logging import get_logger

LOGGER = get_logger(__name__)

#: Events larger than this are written beside the log and referenced by path.
MAX_INLINE_EVENT_BYTES = 16 * 1024

EVENTS_FILENAME = "events.jsonl"
PAYLOAD_DIRNAME = "payloads"


def encode_path_component(value: str) -> str:
    """Make a scope key safe as a single path component.

    Percent-encoding rather than character replacement: ``a:b`` and ``a-b`` would
    collapse to the same directory under replacement, and two different tenants
    sharing a trace directory is the failure this whole module exists to prevent.
    """
    out: list[str] = []
    for char in value:
        if char.isalnum() or char in "-._":
            out.append(char)
        else:
            out.append(f"%{ord(char):02X}")
    return "".join(out)


@dataclass(slots=True)
class StoredEvent:
    event_id: int
    event: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.event_id}\nevent: {self.event}\ndata: {payload}\n\n"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.event_id, "event": self.event, "data": self.data}


class EventLog:
    """Append-only JSONL, one file per run, under a scope-keyed directory."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or get_settings().storage_root)

    def run_dir(self, *, scope_key: str, run_id: UUID | str) -> Path:
        if not scope_key:
            raise PermissionDeniedError(
                "an event log path requires a scope_key; refusing an unscoped path"
            )
        return self.root / encode_path_component(scope_key) / "runs" / str(run_id)

    def events_path(self, *, scope_key: str, run_id: UUID | str) -> Path:
        return self.run_dir(scope_key=scope_key, run_id=run_id) / EVENTS_FILENAME

    def append(
        self,
        *,
        scope_key: str,
        run_id: UUID | str,
        event: str,
        data: dict[str, Any],
        max_inline_bytes: int = MAX_INLINE_EVENT_BYTES,
    ) -> StoredEvent:
        """Append one event and return it with the id it was given.

        Not async: the write is a single small append to a file opened ``O_APPEND``,
        which the kernel serializes, so the API process and a worker can append to
        the same run without coordinating.
        """
        path = self.events_path(scope_key=scope_key, run_id=run_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        event_id = self.latest_id(scope_key=scope_key, run_id=run_id) + 1
        body = dict(data)
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_inline_bytes:
            body = self._offload(
                path.parent, event_id=event_id, event=event, data=data
            )

        line = json.dumps(
            {"id": event_id, "event": event, "ts": datetime.now(UTC).isoformat(), "data": body},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return StoredEvent(event_id=event_id, event=event, data=body)

    def _offload(self, run_dir: Path, *, event_id: int, event: str, data: dict[str, Any]) -> dict:
        payload_dir = run_dir / PAYLOAD_DIRNAME
        payload_dir.mkdir(parents=True, exist_ok=True)
        target = payload_dir / f"{event_id:08d}.json"
        target.write_text(
            json.dumps({"event": event, "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
        LOGGER.info(
            "event payload offloaded",
            extra={"event": event, "event_id": event_id, "path": str(target)},
        )
        return {
            "offloaded_to": str(target.relative_to(run_dir)),
            "bytes": len(json.dumps(data, ensure_ascii=False).encode("utf-8")),
        }

    def latest_id(self, *, scope_key: str, run_id: UUID | str) -> int:
        last = 0
        for stored in self._iter(scope_key=scope_key, run_id=run_id):
            last = stored.event_id
        return last

    def read_after(
        self, *, scope_key: str, run_id: UUID | str, after_id: int = 0, limit: int | None = None
    ) -> list[StoredEvent]:
        out: list[StoredEvent] = []
        for stored in self._iter(scope_key=scope_key, run_id=run_id):
            if stored.event_id <= after_id:
                continue
            out.append(stored)
            if limit is not None and len(out) >= limit:
                break
        return out

    def load_payload(self, *, scope_key: str, run_id: UUID | str, relative: str) -> Any:
        """Read an offloaded body back. Path is joined under the run directory."""
        run_dir = self.run_dir(scope_key=scope_key, run_id=run_id)
        target = (run_dir / relative).resolve()
        if not str(target).startswith(str(run_dir.resolve())):
            raise ValidationError(
                "payload reference escapes the run directory", reference=relative
            )
        return json.loads(target.read_text(encoding="utf-8"))

    def _iter(self, *, scope_key: str, run_id: UUID | str) -> Iterator[StoredEvent]:
        path = self.events_path(scope_key=scope_key, run_id=run_id)
        if not path.exists():
            return
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a process killed mid-write. Skipping it
                    # keeps the rest of the run replayable, which is the whole point.
                    LOGGER.warning("skipping an unparseable event line", extra={"path": str(path)})
                    continue
                yield StoredEvent(
                    event_id=int(record["id"]),
                    event=record["event"],
                    data=record.get("data") or {},
                )


class EventSink:
    """Persist-then-deliver over one run.

    ``emit`` is ordered by the file; ``stream`` replays what it missed and then
    follows the queue, skipping anything replay already covered. A consumer that
    arrives mid-run sees a contiguous id sequence either way.
    """

    def __init__(
        self,
        log: EventLog,
        *,
        scope_key: str,
        run_id: UUID | str,
        max_inline_bytes: int = MAX_INLINE_EVENT_BYTES,
    ) -> None:
        self.log = log
        self.scope_key = scope_key
        self.run_id = run_id
        self.max_inline_bytes = max_inline_bytes
        self._queue: asyncio.Queue[StoredEvent | None] = asyncio.Queue()
        self._emitted: list[StoredEvent] = []
        self._lock = asyncio.Lock()

    async def emit(self, event: str, data: dict[str, Any] | None = None) -> StoredEvent:
        async with self._lock:
            stored = await asyncio.to_thread(
                self.log.append,
                scope_key=self.scope_key,
                run_id=self.run_id,
                event=event,
                data=data or {},
                max_inline_bytes=self.max_inline_bytes,
            )
            # Persisted first, delivered second.
            self._emitted.append(stored)
            await self._queue.put(stored)
            return stored

    def emitted(self) -> list[StoredEvent]:
        return list(self._emitted)

    async def close(self) -> None:
        await self._queue.put(None)

    async def stream(self, *, last_event_id: int | None = None) -> AsyncIterator[StoredEvent]:
        """Replay from the log, then follow live events.

        ``last_event_id`` is the ``Last-Event-ID`` header the browser reconnects
        with; events at or below it are already acknowledged and must not repeat.
        """
        cursor = last_event_id or 0
        for stored in await asyncio.to_thread(
            self.log.read_after, scope_key=self.scope_key, run_id=self.run_id, after_id=cursor
        ):
            cursor = max(cursor, stored.event_id)
            yield stored

        while True:
            stored = await self._queue.get()
            if stored is None:
                # Drain anything emitted between the last replay and the close.
                for pending in self._emitted:
                    if pending.event_id > cursor:
                        yield pending
                return
            if stored.event_id <= cursor:
                continue
            cursor = stored.event_id
            yield stored


def parse_last_event_id(header: str | None) -> int | None:
    """Parse a ``Last-Event-ID`` header, ignoring anything unusable.

    A malformed cursor must not fail the request: the client asked to resume and the
    cheapest safe answer is "here is the whole run" rather than a 422.
    """
    if not header:
        return None
    try:
        value = int(header.strip())
    except ValueError:
        return None
    return value if value >= 0 else None
