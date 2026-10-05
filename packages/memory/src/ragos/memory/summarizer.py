"""Rolling conversation summarization.

The recent ``N`` turns stay verbatim; older turns are compressed into a summary
carrying a recorded ``[start, end)`` window. Two properties matter more than the
compression itself:

**``N`` has no default.** It is a recorded parameter on ``memory_profile_version``
because recursive summarization is the weakest strategy in published memory
benchmarks, and an ``N`` nobody chose is a number nobody measured. Constructing the
summarizer without one raises rather than picking a plausible value.

**Summarization is idempotent per boundary.** Re-running over a window already
covered is a no-op. The writeback is a retried task; a summarizer that appended a
second summary for the same turns would grow the prompt with duplicates on every
retry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.memory.types import CompleteFn

Role = Literal["user", "assistant"]

DEFAULT_SUMMARY_PROMPT = """\
Summarize the conversation so far for someone who will not read it again.

Rules:
- Keep decisions, commitments, names, numbers, and open questions.
- Keep the user's stated preferences and constraints.
- Drop pleasantries and restated text.
- Write at most 8 sentences, plain prose, no preamble.
"""


@dataclass(frozen=True, slots=True)
class Turn:
    index: int
    role: Role
    text: str


@dataclass(slots=True)
class SummaryResult:
    summary: str
    turn_range_start: int
    turn_range_end: int
    version: str
    #: False when the boundary was already covered — the caller must not append
    #: the summary again.
    applied: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.applied


#: Signature of a summarization call.
SummarizeFn = Callable[[str, TenantContext], Awaitable[str]]


class RollingSummarizer:
    """Compress turns older than the verbatim window.

    ``window_n`` is required and ``version`` must name the memory profile that
    recorded it, so a summary can be traced back to the evaluated policy that
    produced it.
    """

    def __init__(
        self,
        *,
        window_n: int | None,
        version: str | None,
        summarize: SummarizeFn | None = None,
        prompt: str = DEFAULT_SUMMARY_PROMPT,
    ) -> None:
        if window_n is None:
            raise ValidationError(
                "rolling summarization requires window_n from a recorded "
                "memory_profile_version; it has no default because the value is an "
                "evaluated parameter",
                field="window_n",
            )
        if window_n < 1:
            raise ValidationError("window_n must be at least 1", window_n=window_n)
        if not version:
            raise ValidationError(
                "rolling summarization requires the memory profile version that "
                "recorded window_n",
                field="version",
                window_n=window_n,
            )
        self.window_n = window_n
        self.version = version
        self.summarize = summarize
        self.prompt = prompt

    def verbatim_turns(self, turns: list[Turn]) -> list[Turn]:
        """The turns that stay uncompressed."""
        if len(turns) <= self.window_n:
            return list(turns)
        return list(turns[-self.window_n :])

    def older_turns(self, turns: list[Turn]) -> list[Turn]:
        if len(turns) <= self.window_n:
            return []
        return list(turns[: -self.window_n])

    async def summarize_older(
        self,
        turns: list[Turn],
        *,
        tenant_ctx: TenantContext,
        already_covered_through: int | None = None,
        previous_summary: str | None = None,
    ) -> SummaryResult:
        """Summarize everything older than the window, once.

        ``already_covered_through`` is the highest turn index a previous summary
        covered; anything at or below it is skipped so a retried writeback does not
        re-summarize the same turns.
        """
        older = self.older_turns(turns)
        if not older:
            return SummaryResult(
                summary=previous_summary or "",
                turn_range_start=0,
                turn_range_end=turns[-1].index if turns else 0,
                version=self.version,
                applied=False,
                notes=["no turns older than the verbatim window"],
            )

        fresh = older
        if already_covered_through is not None:
            fresh = [t for t in older if t.index > already_covered_through]
            if not fresh:
                return SummaryResult(
                    summary=previous_summary or "",
                    turn_range_start=0,
                    turn_range_end=already_covered_through,
                    version=self.version,
                    applied=False,
                    notes=["window already summarized at this boundary"],
                )

        start = fresh[0].index
        end = fresh[-1].index
        if self.summarize is None:
            raise ValidationError(
                "summarization requires a summarization callable; none is wired",
                version=self.version,
            )

        rendered = "\n".join(f"{t.role}: {t.text}" for t in fresh)
        text = await self.summarize(rendered, tenant_ctx)
        summary = (text or "").strip()
        if not summary:
            # An empty summary would silently drop the turns it was supposed to
            # stand in for; keep them verbatim instead.
            return SummaryResult(
                summary=previous_summary or "",
                turn_range_start=0,
                turn_range_end=end,
                version=self.version,
                applied=False,
                notes=["summarizer returned nothing; older turns stay verbatim"],
            )

        if previous_summary:
            summary = f"{previous_summary}\n{summary}"

        return SummaryResult(
            summary=summary,
            turn_range_start=start,
            turn_range_end=end,
            version=self.version,
            applied=True,
        )


def build_summarizer(
    complete: CompleteFn,
    *,
    window_n: int | None,
    version: str | None,
    prompt: str = DEFAULT_SUMMARY_PROMPT,
) -> RollingSummarizer:
    """Wire a gateway completion as the summarizer."""

    async def summarize(text: str, tenant_ctx: TenantContext) -> str:
        return await complete(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            tenant_ctx,
        )

    return RollingSummarizer(window_n=window_n, version=version, summarize=summarize)
