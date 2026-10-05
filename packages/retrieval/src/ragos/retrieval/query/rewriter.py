"""Multi-turn query rewriting.

A follow-up question is not a search query. "And what about the EU version?"
retrieves nothing useful on its own because the entity lives in a previous turn;
the rewrite makes the question standalone before it reaches any recall channel.

The LLM path is injected rather than constructed here so this module stays free
of a provider dependency, and the deterministic path exists because a rewrite
that silently disappears is worse than a mediocre one: it turns a
conversation-shaped question into an unretrievable one with no signal that
anything was lost.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.query.types import ConversationTurn

LOGGER = get_logger(__name__)

#: Signature of a chat completion. The tenant context is a parameter, not
#: ambient state: the gateway charges the call to a tenant and enforces that
#: tenant's budget, so a completion callable that cannot name its tenant is a
#: call with nowhere to meter.
CompleteFn = Callable[[list[dict], TenantContext], Awaitable[str]]

RewriteFn = Callable[[str, tuple[ConversationTurn, ...], TenantContext], Awaitable[str]]

REWRITE_SYSTEM_PROMPT = """\
You rewrite a follow-up question into a standalone search query.

Rules:
- Resolve every pronoun and elliptical reference using the conversation.
- Keep the original language of the question.
- Output the search query only, one line, no quotes, no explanation.
- If the question is already standalone, repeat it unchanged.
"""

DEFAULT_HISTORY_TURNS = 4
#: A rewrite longer than this is a summarization, not a query. Long inputs also
#: degrade badly on the embedding side.
MAX_REWRITE_CHARS = 2000


def default_history(
    turns: tuple[ConversationTurn, ...], limit: int
) -> tuple[ConversationTurn, ...]:
    """Most recent ``limit`` turns, oldest first.

    Truncating from the front keeps the immediately preceding exchange, which
    carries the referent; dropping the tail would discard exactly the turn the
    follow-up points back to.
    """
    return turns[-limit:] if limit > 0 else ()


class ContextualRewriter:
    """Turn a context-dependent question into a standalone one."""

    def __init__(
        self,
        complete: CompleteFn | None = None,
        *,
        max_history_turns: int = DEFAULT_HISTORY_TURNS,
    ) -> None:
        self.complete = complete
        self.max_history_turns = max_history_turns

    async def rewrite(
        self,
        query: str,
        history: tuple[ConversationTurn, ...],
        tenant_ctx: TenantContext,
    ) -> str:
        if not history:
            return query

        turns = default_history(history, self.max_history_turns)
        if self.complete is not None:
            rewritten = await self._rewrite_with_llm(query, turns, tenant_ctx)
            if rewritten:
                return rewritten
            LOGGER.warning(
                "query rewrite returned nothing usable; using deterministic fallback",
                extra={"query": query[:120]},
            )
        return self._rewrite_deterministically(query, turns)

    async def _rewrite_with_llm(
        self,
        query: str,
        turns: tuple[ConversationTurn, ...],
        tenant_ctx: TenantContext,
    ) -> str:
        transcript = "\n".join(f"{t.role}: {t.text}" for t in turns)
        try:
            raw = await self.complete(
                [
                    {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Conversation:\n{transcript}\n\nQuestion: {query}",
                    },
                ],
                tenant_ctx,
            )
        except Exception:
            LOGGER.exception("query rewrite call failed; using deterministic fallback")
            return ""
        # A whitespace-only reply strips to nothing, so the first line has to be
        # looked up defensively rather than indexed.
        lines = (raw or "").strip().splitlines()
        candidate = lines[0].strip() if lines else ""
        if not candidate:
            return ""
        return candidate[:MAX_REWRITE_CHARS]

    @staticmethod
    def _rewrite_deterministically(
        query: str, turns: tuple[ConversationTurn, ...]
    ) -> str:
        """Prepend prior user turns.

        Degraded by construction — no coreference is resolved — but it keeps the
        entity words in the query, and lexical recall can then match them. The
        failure mode is weaker ranking, not zero recall.
        """
        prior = [t.text.strip() for t in turns if t.role == "user" and t.text.strip()]
        if not prior:
            return query
        context = " ".join(prior[-2:])
        return f"{context} {query}".strip()[:MAX_REWRITE_CHARS]
