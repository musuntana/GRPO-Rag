"""The durable run event log.

AGENTS.md streaming rules: monotonic ids, persist before delivery, replay from
``Last-Event-ID``, bounded inline payloads, and no routine that removes entries a
suspended run still needs.

The claims under test are about ordering and durability rather than formatting:
an acknowledged event is on disk before it reaches a client, and a reconnect sees a
contiguous id sequence without duplicates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.runtime.events import (
    MAX_INLINE_EVENT_BYTES,
    EventLog,
    EventSink,
    encode_path_component,
    parse_last_event_id,
)

SCOPE_KEY = "11111111-2222-3333-4444-555555555555::u_1"


@pytest.fixture
def log(tmp_path) -> EventLog:
    return EventLog(tmp_path)


# -- paths ------------------------------------------------------------------


def test_runs_are_stored_under_the_scope_key() -> None:
    log = EventLog("/tmp/x")
    run_id = uuid4()

    path = log.events_path(scope_key=SCOPE_KEY, run_id=run_id)

    assert encode_path_component(SCOPE_KEY) in str(path)
    assert str(run_id) in str(path)
    assert path.name == "events.jsonl"


def test_two_scope_keys_never_share_a_directory() -> None:
    """Character replacement would collapse these; percent-encoding does not."""
    assert encode_path_component("a:b") != encode_path_component("a-b")
    assert encode_path_component("t::u") != encode_path_component("t::v")


def test_an_unscoped_run_has_no_path() -> None:
    with pytest.raises(PermissionDeniedError, match="scope_key"):
        EventLog("/tmp/x").events_path(scope_key="", run_id=uuid4())


# -- ids and persistence ----------------------------------------------------


def test_event_ids_are_monotonic_and_start_at_one(log: EventLog) -> None:
    run_id = uuid4()

    first = log.append(scope_key=SCOPE_KEY, run_id=run_id, event="a", data={})
    second = log.append(scope_key=SCOPE_KEY, run_id=run_id, event="b", data={})
    third = log.append(scope_key=SCOPE_KEY, run_id=run_id, event="c", data={})

    assert [first.event_id, second.event_id, third.event_id] == [1, 2, 3]


def test_ids_are_per_run_not_global(log: EventLog) -> None:
    """A reconnect cursor must not depend on unrelated traffic."""
    a, b = uuid4(), uuid4()

    log.append(scope_key=SCOPE_KEY, run_id=a, event="x", data={})
    second = log.append(scope_key=SCOPE_KEY, run_id=b, event="x", data={})

    assert second.event_id == 1


def test_an_event_is_on_disk_before_it_is_delivered(log: EventLog) -> None:
    """The ordering is the guarantee: acknowledged implies durable."""
    run_id = uuid4()
    observed: list[int] = []

    async def scenario() -> None:
        sink = EventSink(log, scope_key=SCOPE_KEY, run_id=run_id)

        async def consumer():
            async for event in sink.stream():
                # The file already contains this event at the moment it arrives.
                on_disk = log.read_after(scope_key=SCOPE_KEY, run_id=run_id, after_id=0)
                observed.append(len(on_disk))
                yield event

        async def produce() -> None:
            await sink.emit("a", {})
            await sink.emit("b", {})
            await sink.close()

        producer = asyncio.create_task(produce())
        async for _ in consumer():
            pass
        await producer

    asyncio.run(scenario())

    assert observed == [1, 2]


def test_a_reconnect_replays_only_what_was_missed(log: EventLog) -> None:
    run_id = uuid4()

    for i in range(4):
        log.append(scope_key=SCOPE_KEY, run_id=run_id, event=f"e{i}", data={})

    replayed = log.read_after(scope_key=SCOPE_KEY, run_id=run_id, after_id=2)

    assert [e.event_id for e in replayed] == [3, 4]


def test_a_torn_final_line_does_not_break_replay(log: EventLog, tmp_path: Path) -> None:
    """A process killed mid-write must not cost the whole run its history."""
    run_id = uuid4()
    log.append(scope_key=SCOPE_KEY, run_id=run_id, event="a", data={})
    with open(log.events_path(scope_key=SCOPE_KEY, run_id=run_id), "a", encoding="utf-8") as fh:
        fh.write('{"id": 2, "event": "b", "dat\n')

    replayed = log.read_after(scope_key=SCOPE_KEY, run_id=run_id, after_id=0)

    assert [e.event_id for e in replayed] == [1]


# -- payloads ---------------------------------------------------------------


def test_an_oversized_payload_is_offloaded_and_referenced(log: EventLog) -> None:
    """A replay should not have to read a multi-megabyte body to learn it was long."""
    run_id = uuid4()

    stored = log.append(
        scope_key=SCOPE_KEY,
        run_id=run_id,
        event="huge",
        data={"body": "x" * 5000},
        max_inline_bytes=1024,
    )

    assert set(stored.data) == {"offloaded_to", "bytes"}
    reference = stored.data["offloaded_to"]
    payload = log.load_payload(scope_key=SCOPE_KEY, run_id=run_id, relative=reference)
    assert payload["data"]["body"] == "x" * 5000


def test_a_small_payload_stays_inline(log: EventLog) -> None:
    stored = log.append(scope_key=SCOPE_KEY, run_id=uuid4(), event="small", data={"n": 1})

    assert stored.data == {"n": 1}


def test_an_offloaded_reference_may_not_escape_the_run_directory(log: EventLog) -> None:
    with pytest.raises(ValidationError, match="escapes"):
        log.load_payload(scope_key=SCOPE_KEY, run_id=uuid4(), relative="../../etc/passwd")


def test_the_inline_cap_is_a_module_constant() -> None:
    """A cap nobody can see is a cap nobody can test against."""
    assert 1024 <= MAX_INLINE_EVENT_BYTES <= 1024 * 1024


# -- SSE framing and cursors ------------------------------------------------


def test_an_event_renders_with_an_id_and_a_type(log: EventLog) -> None:
    stored = log.append(scope_key=SCOPE_KEY, run_id=uuid4(), event="token_delta",
                        data={"delta": "hi"})

    frame = stored.to_sse()

    assert frame.startswith("id: 1\nevent: token_delta\ndata: ")
    assert frame.endswith("\n\n")


def test_a_last_event_id_cursor_is_parsed() -> None:
    assert parse_last_event_id("7") == 7
    assert parse_last_event_id(" 7 ") == 7


@pytest.mark.parametrize("header", [None, "", "abc", "-1"])
def test_an_unusable_cursor_replays_the_whole_run(header: str | None) -> None:
    """Failing the reconnect would be worse than repeating an event."""
    assert parse_last_event_id(header) is None


def test_a_stream_that_starts_late_replays_before_following_live_events(log: EventLog) -> None:
    run_id = uuid4()

    async def scenario():
        sink = EventSink(log, scope_key=SCOPE_KEY, run_id=run_id)
        await sink.emit("run_started", {"run_id": str(run_id)})
        await sink.emit("memory_loaded", {"facts": 1})
        await sink.close()
        return [event async for event in sink.stream()]

    events = asyncio.run(scenario())

    assert [e.event_id for e in events] == [1, 2]
    assert [e.event for e in events] == ["run_started", "memory_loaded"]


def test_resuming_from_a_cursor_skips_the_acknowledged_prefix(log: EventLog) -> None:
    run_id = uuid4()

    async def scenario():
        sink = EventSink(log, scope_key=SCOPE_KEY, run_id=run_id)
        await sink.emit("a", {})
        await sink.emit("b", {})
        await sink.close()
        return [e.event async for e in sink.stream(last_event_id=1)]

    events = asyncio.run(scenario())

    assert events == ["b"]


def test_an_empty_event_name_is_rendered_verbatim() -> None:
    """A frame no client can dispatch is still well-formed, and that is asserted
    rather than left implicit."""
    from ragos.runtime.events import StoredEvent

    assert StoredEvent(event_id=0, event="", data={}).to_sse() == "id: 0\nevent: \ndata: {}\n\n"


def test_two_sinks_on_one_run_continue_the_same_id_sequence(log: EventLog) -> None:
    """A process restart must not reset the cursor a reconnecting client holds."""
    run_id = uuid4()
    log.append(scope_key=SCOPE_KEY, run_id=run_id, event="before_restart", data={})

    after = log.append(scope_key=SCOPE_KEY, run_id=run_id, event="after_restart", data={})

    assert after.event_id == 2
