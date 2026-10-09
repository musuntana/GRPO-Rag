"""Memory orchestration: build the context, and schedule the writeback.

Two behaviours here are contracts rather than implementation:

**Memory is injected as its own labelled section.** Never mixed into the
knowledge-base evidence. A fact and a passage look identical to a model once they
are concatenated into one prompt, and the resulting error — an assertion about the
user stated as if it came from the documentation — is invisible in the output
while being the whole point of keeping the tiers apart.

**Writeback is a durable task, never fire-and-forget.** Fact extraction and
summarization run after the response is delivered; doing them inline adds seconds
of LLM latency to a chat turn for work the user did not wait on. The failure mode
to avoid is the other one too — an inline call that fails leaves the memory
silently empty with no retry and no record. So: a task row with an idempotency key
derived from the conversation, claimed by the worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.memory.fact_store import MemoryFactStore
from ragos.memory.rerank import CrossEncoderFn, rank_facts
from ragos.memory.summarizer import SummaryResult, Turn
from ragos.memory.types import CompleteFn, ExtractedFact, MemoryFact

LOGGER = get_logger(__name__)

#: Task kind for the durable writeback.
MEMORY_WRITEBACK = "memory_writeback"

#: The section label. Kept as a constant so the prompt builder and any renderer of
#: the context agree on it.
MEMORY_SECTION_LABEL = "Long-term memory about this user (from earlier conversations)"


@dataclass(slots=True)
class MemoryContext:
    """What generation is allowed to know about the user.

    Three sources, kept distinguishable: facts (cross-conversation), the rolling
    summary (this conversation, compressed), and recent turns (this conversation,
    verbatim).
    """

    facts: list[MemoryFact] = field(default_factory=list)
    conversation_summary: str | None = None
    recent_messages: list[Turn] = field(default_factory=list)
    user_id: UUID | None = None

    def is_empty(self) -> bool:
        return not (self.facts or self.conversation_summary or self.recent_messages)

    def render(self) -> str:
        """Render as a labelled block, or an empty string when there is nothing.

        An empty block is returned rather than a placeholder heading: a section that
        says "no memory" invites the model to comment on the absence.
        """
        blocks: list[str] = []
        if self.facts:
            lines = [f"- {fact.fact}" for fact in self.facts]
            blocks.append(f"{MEMORY_SECTION_LABEL}:\n" + "\n".join(lines))
        if self.conversation_summary:
            blocks.append(f"Earlier in this conversation:\n{self.conversation_summary}")
        if self.recent_messages:
            lines = [f"{turn.role}: {turn.text}" for turn in self.recent_messages]
            blocks.append("Recent turns:\n" + "\n".join(lines))
        return "\n\n".join(blocks)


@dataclass(slots=True)
class WritebackRequest:
    conversation_id: UUID
    user_id: UUID
    message_count: int
    window_n: int
    profile_version: str


class MemoryManager:
    def __init__(
        self,
        *,
        fact_store: MemoryFactStore,
        complete: CompleteFn | None = None,
        cross_encoder: CrossEncoderFn | None = None,
        fact_limit: int = 10,
    ) -> None:
        self.fact_store = fact_store
        self.complete = complete
        self.cross_encoder = cross_encoder
        self.fact_limit = fact_limit

    @property
    def tenant_ctx(self) -> TenantContext:
        return self.fact_store.tenant_ctx

    # -- read ---------------------------------------------------------------

    async def build_context(
        self,
        *,
        user_id: UUID | None,
        conversation_id: UUID | None = None,
        recent_turns: Sequence[Turn] = (),
        conversation_summary: str | None = None,
        query: str = "",
        now: datetime | None = None,
    ) -> MemoryContext:
        """Assemble the memory context for one turn.

        ``user_id`` is required. Without an actor there is no memory to return and
        no way to scope what does exist, so this refuses rather than falling back to
        the tenant's facts.
        """
        owner = self.tenant_ctx.require_user_id() if user_id is None else user_id
        scope_key = self.tenant_ctx.require_private_scope_key()
        moment = now or datetime.now(UTC)

        candidates = self.fact_store.recall(
            scope_key=scope_key, user_id=owner, limit=self.fact_limit * 4, now=moment
        )
        ranked = await rank_facts(
            query or "",
            candidates,
            cross_encoder=self.cross_encoder,
            top_n=self.fact_limit,
        )
        # A fact handed to the prompt was used; its recency clock resets. Skipping
        # this makes decay a function of ingestion time alone, which is the
        # standalone counter the design rejects.
        used = [item.fact.id for item in ranked]
        if used:
            self.fact_store.touch(scope_key=scope_key, user_id=owner, fact_ids=used, now=moment)

        return MemoryContext(
            facts=[item.fact for item in ranked],
            conversation_summary=conversation_summary,
            recent_messages=list(recent_turns),
            user_id=owner,
        )

    # -- write --------------------------------------------------------------

    def enqueue_writeback(
        self,
        *,
        task_repo,
        conversation_id: UUID,
        user_id: UUID | None,
        message_count: int,
        window_n: int,
        profile_version: str,
        available_at: datetime | None = None,
    ):
        """Insert a durable writeback task.

        The idempotency key is derived from the conversation and the message count,
        so a retried enqueue and a retried delivery both land on one row. Fire and
        forget is the alternative this replaces: it loses facts silently and leaves
        nothing to retry.
        """
        owner = self.tenant_ctx.require_user_id() if user_id is None else user_id
        scope_key = self.tenant_ctx.require_private_scope_key()
        if window_n < 1:
            raise ValidationError(
                "writeback requires a recorded window_n from a memory profile version",
                field="window_n",
            )

        request = WritebackRequest(
            conversation_id=conversation_id,
            user_id=owner,
            message_count=message_count,
            window_n=window_n,
            profile_version=profile_version,
        )
        return task_repo.enqueue(
            kind=MEMORY_WRITEBACK,
            payload={
                "conversation_id": str(request.conversation_id),
                "user_id": str(request.user_id),
                "message_count": request.message_count,
                "window_n": request.window_n,
                "profile_version": request.profile_version,
                "scope_key": scope_key,
            },
            idempotency_key=f"memory-writeback:{conversation_id}:{message_count}",
            available_at=available_at,
        )

    async def writeback(
        self,
        *,
        user_id: UUID,
        transcript: Sequence[dict],
        summarizer,
        conversation_id: UUID | None = None,
        turns: Sequence[Turn] = (),
        already_covered_through: int | None = None,
        now: datetime | None = None,
    ) -> tuple[list[ExtractedFact], SummaryResult | None]:
        """Extract facts and roll the summary. Called by the worker, not the request."""
        scope_key = self.tenant_ctx.require_private_scope_key()
        moment = now or datetime.now(UTC)

        facts: list[ExtractedFact] = []
        if self.complete is not None:
            from ragos.memory.extractor import FactExtractor

            facts = await FactExtractor(self.complete).extract(
                list(transcript), tenant_ctx=self.tenant_ctx
            )
        stored = self.fact_store.record(
            scope_key=scope_key,
            user_id=user_id,
            facts=facts,
            conversation_id=conversation_id,
            now=moment,
        )

        summary: SummaryResult | None = None
        if summarizer is not None and turns:
            summary = await summarizer.summarize_older(
                list(turns),
                tenant_ctx=self.tenant_ctx,
                already_covered_through=already_covered_through,
            )

        self._log_writeback(stored, summary, facts)
        return stored, summary

    def writeback_sync(
        self,
        *,
        user_id: UUID,
        transcript: Sequence[dict],
        summarizer,
        conversation_id: UUID | None = None,
        turns: Sequence[Turn] = (),
        already_covered_through: int | None = None,
        now: datetime | None = None,
    ) -> tuple[list[ExtractedFact], SummaryResult | None]:
        """Synchronous bridge for the task worker.

        The writeback itself is async because extraction and summarization are model
        calls; the worker dispatch loop is synchronous. This is the only place the
        two meet, so an event loop cannot already be running here — and the request
        path awaits :meth:`writeback` directly instead.
        """
        return asyncio.run(
            self.writeback(
                user_id=user_id,
                transcript=transcript,
                summarizer=summarizer,
                conversation_id=conversation_id,
                turns=turns,
                already_covered_through=already_covered_through,
                now=now,
            )
        )

    def _log_writeback(
        self, stored: Sequence[MemoryFact], summary: SummaryResult | None, facts: Sequence
    ) -> None:
        LOGGER.info(
            "memory writeback complete",
            extra={
                "facts_proposed": len(facts),
                "facts_stored": len(stored),
                "summary_applied": bool(summary and summary.applied),
                "trace_id": self.tenant_ctx.trace_id,
            },
        )
