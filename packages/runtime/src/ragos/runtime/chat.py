"""Chat orchestration: memory in, citations out, one durable event log.

The order of operations is the contract:

1. resolve the actor — without one, the request is refused rather than served
   without memory, because "no memory" and "the tenant's memory" are different
   answers and only one of them was asked for
2. build the memory context: facts, rolling summary, recent turns
3. run the retrieval chain, with memory as one channel among several
4. stream the answer as SSE events
5. persist both messages with their citations
6. schedule the memory writeback as a durable task

Two honest deviations from the illustrative protocol in ``docs/implementation.md``:

- **Token deltas are segmentation, not token streaming.** The provider interface
  returns a complete message, so ``token_delta`` events are emitted by walking the
  finished text. They are a rendering convenience; the run trace is the record.
- **There is no ``memory_updated`` event.** The writeback is asynchronous, so an
  event claiming memory was updated would be asserting something that has not
  happened yet and that may fail. ``memory_writeback_scheduled`` reports what is
  true at the moment it is emitted.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.memory.manager import MemoryManager
from ragos.memory.summarizer import Turn
from ragos.retrieval.pipeline import RAGResult
from ragos.runtime.events import EventSink, StoredEvent
from ragos.runtime.retrieval import RetrievalService
from ragos.storage.repositories.conversations import MessageRepository
from ragos.storage.repositories.tasks import TaskRepository
from sqlmodel import Session

LOGGER = get_logger(__name__)

#: Segmentation of a finished answer into SSE deltas.
#:
#: CJK-aware, and the ordering of the alternatives is the whole mechanism: a
#: single ideograph is tried first because ``\S+`` would otherwise swallow a whole
#: Chinese sentence as one token — there is no whitespace to split on there. Latin
#: text still segments per word.
_DELTA_PATTERN = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
    r"|\S+|\s+",
    re.UNICODE,
)
MAX_DELTA_CHARS = 240


@dataclass(slots=True)
class ChatDependencies:
    """Everything the service needs, injected.

    Assembling dependencies at the call site keeps the orchestration testable
    without a gateway, a vector store, or a model — and keeps the provider calls
    visible where they are made.
    """

    retrieval: RetrievalService
    memory: MemoryManager
    sink: EventSink
    messages: MessageRepository
    tasks: TaskRepository
    retriever_profile_id: UUID
    window_n: int
    memory_profile_version: str
    retriever_profile_version: int | None = None


class ChatService:
    """Runs one chat turn and emits its events."""

    def __init__(
        self,
        *,
        session: Session,
        tenant_ctx: TenantContext,
        deps: ChatDependencies,
    ) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx
        self.deps = deps

    @property
    def scope_key(self) -> str:
        return self.tenant_ctx.require_scope_key()

    async def run_turn(
        self,
        *,
        conversation_id: UUID,
        user_message: str,
        run_id: UUID | None = None,
    ) -> None:
        """Produce the turn's events into the sink.

        Not an iterator over the events. A generator can be consumed once, which is
        the wrong shape for a stream that has to survive a disconnect: the events
        are written to the run's log and the sink is drained from there, so a
        reconnecting client reads the same run a second time.
        """
        text = (user_message or "").strip()
        if not text:
            raise ValidationError("message must not be empty", field="message")

        run = run_id or uuid4()
        sink = self.deps.sink
        user_id = self.tenant_ctx.require_user_id()
        started = time.monotonic()

        await sink.emit(
            "run_started",
            {"run_id": str(run), "conversation_id": str(conversation_id)},
        )

        history = self._load_history(conversation_id)
        self.deps.messages.append(
            scope_key=self.scope_key,
            conversation_id=conversation_id,
            role="user",
            content=text,
            run_id=run,
        )

        memory = await self.deps.memory.build_context(
            user_id=user_id,
            conversation_id=conversation_id,
            recent_turns=[
                Turn(index=idx, role=m.role, text=m.content)  # type: ignore[arg-type]
                for idx, m in enumerate(history[-4:], start=max(0, len(history) - 4))
            ],
            query=text,
        )
        if memory.facts or memory.conversation_summary:
            await sink.emit(
                "memory_loaded",
                {
                    "facts": len(memory.facts),
                    "has_summary": memory.conversation_summary is not None,
                    "recent_turns": len(memory.recent_messages),
                },
            )

        outcome = await self.deps.retrieval.query(
            query=text,
            profile_id=self.deps.retriever_profile_id,
            profile_version=self.deps.retriever_profile_version,
        )
        result = outcome.result

        await self._emit_retrieval_events(result)
        for delta in _segment(result.answer):
            await sink.emit("token_delta", {"delta": delta})

        await sink.emit(
            "citation_linked",
            {
                "citations": [c.to_dict() for c in result.citations],
                "supported": result.retrieval_explain.citation_supported,
                "generated": result.retrieval_explain.citation_generated,
            },
        )

        answer_message = self.deps.messages.append(
            scope_key=self.scope_key,
            conversation_id=conversation_id,
            role="assistant",
            content=result.answer,
            run_id=run,
            citations=[c.to_dict() for c in result.citations],
        )

        message_count = len(history) + 2
        task = self.deps.memory.enqueue_writeback(
            task_repo=self.deps.tasks,
            conversation_id=conversation_id,
            user_id=user_id,
            message_count=message_count,
            window_n=self.deps.window_n,
            profile_version=self.deps.memory_profile_version,
        )
        await sink.emit(
            "memory_writeback_scheduled",
            {"task_id": str(task.id), "message_count": message_count},
        )

        await sink.emit(
            "run_finished",
            {
                "run_id": str(run),
                "message_id": str(answer_message.id),
                "abstained": result.abstained,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "warnings": outcome.warnings,
            },
        )
        await sink.close()

    async def _emit_retrieval_events(self, result: RAGResult) -> None:
        explain = result.retrieval_explain
        await self.deps.sink.emit(
            "retrieval_completed",
            {
                "channels": len(explain.per_channel_counts),
                "per_channel_counts": explain.per_channel_counts,
                "strategy": explain.query_strategy,
            },
        )
        await self.deps.sink.emit(
            "rerank_completed",
            {
                "top_k": explain.rerank_depth,
                "model_profile": explain.rerank_model_profile,
                "parents_resolved": explain.parents_resolved,
            },
        )
        await self.deps.sink.emit(
            "relevance_graded",
            {
                "verdict": explain.gate_verdict,
                "confidence": explain.gate_confidence,
                "action": explain.gate_action,
                "rails_prompt_id": explain.rails_prompt_id,
            },
        )

    def _load_history(self, conversation_id: UUID) -> list:
        return self.deps.messages.list_for_conversation(
            scope_key=self.scope_key, conversation_id=conversation_id
        )


def _segment(answer: str) -> Sequence[str]:
    """Split a finished answer into renderable deltas.

    Bounded so one pathological token cannot produce a single unbounded SSE frame,
    and CJK-aware because there is no whitespace to split on.
    """
    out: list[str] = []
    for piece in _DELTA_PATTERN.findall(answer or ""):
        while len(piece) > MAX_DELTA_CHARS:
            out.append(piece[:MAX_DELTA_CHARS])
            piece = piece[MAX_DELTA_CHARS:]
        if piece:
            out.append(piece)
    return out


async def drain(sink: EventSink, *, last_event_id: int | None = None) -> list[StoredEvent]:
    """Collect a sink's events. Used by tests and by non-streaming callers."""
    return [event async for event in sink.stream(last_event_id=last_event_id)]
