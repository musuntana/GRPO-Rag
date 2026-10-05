"""Query strategy selection.

Retrieval runs on text, not on the user's literal question. This module decides
what that text is:

    direct     — the question, rewritten for context when there is history
    hyde       — a hypothetical answer document, for vague or short questions
    decompose  — one query per sub-intent, merged by the fusion layer

The strategy comes from the retriever profile. Choosing it per query would make
retrieval quality depend on a classifier nobody evaluated, and the eval numbers
would then describe a mixture of three configurations rather than one.
"""

from __future__ import annotations

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.query.decomposer import Decomposer
from ragos.retrieval.query.hyde import HyDE, is_informative
from ragos.retrieval.query.rewriter import ContextualRewriter
from ragos.retrieval.query.types import ConversationTurn, QueryPlan

LOGGER = get_logger(__name__)

STRATEGIES = ("direct", "hyde", "decompose")


class QueryEngine:
    """Produce a :class:`QueryPlan` for one question.

    Every strategy degrades rather than fails: a missing completion callable, an
    empty hypothetical, or a decomposition that returns nothing all fall back to
    the direct path. The alternative — failing the request — turns a degraded
    quality into a missing answer, and the degradation is recorded in
    ``QueryPlan.notes`` so the explain shows what actually happened.
    """

    def __init__(
        self,
        *,
        rewriter: ContextualRewriter | None = None,
        hyde: HyDE | None = None,
        decomposer: Decomposer | None = None,
    ) -> None:
        self.rewriter = rewriter or ContextualRewriter()
        self.hyde = hyde
        self.decomposer = decomposer

    async def plan(
        self,
        query: str,
        tenant_ctx: TenantContext,
        *,
        strategy: str = "direct",
        history: tuple[ConversationTurn, ...] | None = None,
    ) -> QueryPlan:
        text = (query or "").strip()
        if not text:
            raise ValidationError("query must not be empty", field="query")

        if strategy not in STRATEGIES:
            raise ValidationError(
                "unknown query strategy", strategy=strategy, supported=list(STRATEGIES)
            )

        turns = history or ()
        primary = await self.rewriter.rewrite(text, turns, tenant_ctx) if turns else text
        used_history = bool(turns) and primary != text

        if strategy == "hyde":
            return await self._plan_hyde(primary, used_history, tenant_ctx)
        if strategy == "decompose":
            return await self._plan_decompose(primary, used_history, tenant_ctx)
        return QueryPlan(
            strategy="direct",
            primary=primary,
            queries=(primary,),
            used_history=used_history,
        )

    async def _plan_hyde(
        self, primary: str, used_history: bool, tenant_ctx: TenantContext
    ) -> QueryPlan:
        notes: list[str] = []
        hypothetical = await self.hyde.generate(primary, tenant_ctx) if self.hyde else None
        recall_query = primary

        if hypothetical and is_informative(hypothetical, primary):
            recall_query = hypothetical
        elif hypothetical:
            notes.append("hyde passage restated the question; retrieving the question")
        else:
            notes.append("hyde unavailable; retrieving the question")

        return QueryPlan(
            strategy="hyde",
            # Grading, refinement, and generation stay on the real question:
            # a hypothetical document graded against a hypothetical query tells
            # you nothing about whether the evidence answers the user.
            primary=primary,
            queries=(recall_query,),
            hypothetical=hypothetical,
            used_history=used_history,
            notes=notes,
        )

    async def _plan_decompose(
        self, primary: str, used_history: bool, tenant_ctx: TenantContext
    ) -> QueryPlan:
        notes: list[str] = []
        if self.decomposer is None:
            return QueryPlan(
                strategy="direct",
                primary=primary,
                queries=(primary,),
                used_history=used_history,
                notes=["decompose requested without a decomposer; retrieving the question"],
            )

        sub_queries = await self.decomposer.decompose(primary, tenant_ctx)
        if len(sub_queries) <= 1:
            notes.append("single-intent question; decomposition added nothing")
            return QueryPlan(
                strategy="direct",
                primary=primary,
                queries=(primary,),
                sub_queries=sub_queries,
                used_history=used_history,
                notes=notes,
            )

        return QueryPlan(
            strategy="decompose",
            primary=primary,
            queries=sub_queries,
            sub_queries=sub_queries,
            used_history=used_history,
            notes=notes,
        )
