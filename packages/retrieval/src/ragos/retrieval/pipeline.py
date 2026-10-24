"""Full retrieval chain orchestrator.

    query -> rewrite -> recall (parallel) -> fuse -> rerank
          -> relevance gate -> generate -> verify citations

Every stage takes the resolved ``tenant_ctx`` and reads ``scope_key`` from it;
no stage reads tenant scope from ambient state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import zip_longest
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import Citation, EvidenceSet, bind_markers
from ragos.retrieval.expander import (
    dedupe_overlapping_parents,
    expand_to_parents,
)
from ragos.retrieval.fusion import Fusioner, allocate_by_budget
from ragos.retrieval.grader import GateAction, GateResult, RelevanceGrader
from ragos.retrieval.query.engine import QueryEngine
from ragos.retrieval.query.types import ConversationTurn, QueryPlan
from ragos.retrieval.recall.parents import ParentResolver, ResolvedParents
from ragos.retrieval.refine import RefineFn, RefineRequest, deterministic_refine
from ragos.retrieval.reranker import RerankFn, RerankResult, rerank, select_evidence
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.retrieval.verify import EntailmentFn, citation_counts, verify_citations

LOGGER = get_logger(__name__)

#: One recall channel: takes the query plus that channel's allocation.
RecallFn = Callable[[str, RetrieverProfile, TenantContext], Awaitable[list[ScoredChunk]]]

#: Generation returns the answer text; citations are bound from markers against
#: the evidence actually delivered, so a marker cannot name a page the model
#: never saw.
GenerateFn = Callable[[str, list[ScoredChunk], TenantContext], Awaitable[str]]


@dataclass(slots=True)
class RetrievalExplain:
    """Per-node trace. A quality regression is localized to the stage that caused
    it, which is only possible if each stage records what it saw."""

    rewritten_query: str
    per_channel_counts: dict[str, int] = field(default_factory=dict)
    channel_budgets: dict[str, float] = field(default_factory=dict)
    fusion_scores: dict[str, float] = field(default_factory=dict)
    rerank_depth: int = 0
    rerank_model_profile: str | None = None
    gate_verdict: str | None = None
    gate_confidence: float | None = None
    gate_action: str | None = None
    rails_prompt_id: str | None = None
    citations: list[dict] = field(default_factory=list)
    citation_supported: int = 0
    citation_generated: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    query_strategy: str = "direct"
    #: Every text the recall channels actually ran, in order.
    recall_queries: list[str] = field(default_factory=list)
    sub_queries: list[str] = field(default_factory=list)
    used_history: bool = False
    plan_notes: list[str] = field(default_factory=list)
    #: Children → parent expansion. ``0`` means the run had no parent source, which
    #: is a different state from "expansion found nothing to add".
    parents_resolved: int = 0
    #: Output of the shared refine node. Always populated — all three gate paths
    #: route through it, and an absent value here means a branch built its own
    #: context.
    refined_query: str | None = None
    re_retrieval_attempted: bool = False
    re_retrieval_added_chunks: int = 0


@dataclass(slots=True)
class RAGResult:
    answer: str
    citations: list[Citation]
    retrieval_explain: RetrievalExplain
    evidence: tuple[ScoredChunk, ...]
    gate: GateResult | None
    abstained: bool
    token_usage: dict[str, int]
    query_plan: QueryPlan | None = None


class RAGPipeline:
    def __init__(
        self,
        *,
        recall_channels: dict[str, RecallFn],
        generator: GenerateFn,
        grader: RelevanceGrader | None = None,
        reranker: RerankFn | None = None,
        entailment: EntailmentFn | None = None,
        parents: dict[UUID, ScoredChunk] | None = None,
        children_by_parent: dict[UUID, list[UUID]] | None = None,
        query_engine: QueryEngine | None = None,
        refiner: RefineFn | None = None,
        parent_resolver: ParentResolver | None = None,
    ) -> None:
        self.recall_channels = recall_channels
        self.generator = generator
        self.grader = grader
        self.reranker = reranker
        self.entailment = entailment
        self.parents = parents or {}
        self.children_by_parent = children_by_parent or {}
        self.fusioner = Fusioner()
        self.query_engine = query_engine or QueryEngine()
        self.refiner = refiner
        self.parent_resolver = parent_resolver

    async def run(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
        *,
        conversation_history: list[ConversationTurn] | list[str] | None = None,
    ) -> RAGResult:
        tenant_ctx.require_scope_key()
        profile.validate()

        plan = await self.query_engine.plan(
            query,
            tenant_ctx,
            strategy=profile.query_strategy,
            history=_as_turns(conversation_history),
        )
        explain = RetrievalExplain(
            rewritten_query=plan.primary,
            query_strategy=plan.strategy,
            recall_queries=list(plan.queries),
            sub_queries=list(plan.sub_queries),
            used_history=plan.used_history,
            plan_notes=list(plan.notes),
        )

        ranked = await self._retrieve_and_rerank(plan, profile, tenant_ctx)
        explain.per_channel_counts = {name: len(chunks) for name, chunks in ranked.channels.items()}
        explain.channel_budgets = dict(profile.channel_budgets)
        explain.fusion_scores = {
            str(c.chunk_id): c.score for c in ranked.reranked.ranked[: profile.top_k_rerank]
        }
        explain.rerank_depth = ranked.reranked.depth
        explain.rerank_model_profile = ranked.reranked.rerank_model_profile

        gate: GateResult | None = None
        if profile.crag_enabled and self.grader is not None:
            gate = await self.grader.grade(plan.primary, ranked.reranked.ranked, tenant_ctx)
            explain.gate_verdict = gate.verdict
            explain.gate_confidence = gate.confidence
            explain.gate_action = gate.action
            explain.rails_prompt_id = gate.prompt_id

        # One refine node, three gate paths. The ambiguous path additionally
        # re-retrieves once with the refined sentence; a second attempt is not
        # made, because a gate that has already said "ambiguous" twice has said
        # the corpus does not contain the answer.
        refined = await self._refine(plan.primary, gate, ranked.reranked.ranked, tenant_ctx)
        explain.refined_query = refined

        if gate is not None and gate.action == GateAction.REFUSE:
            # The refuse path routes through the same evidence-assembly shape as
            # the others. A branch that builds its own context is how the
            # refusal path starts returning half-answers.
            LOGGER.info(
                "relevance gate refused; abstaining",
                extra={"confidence": gate.confidence, "trace_id": tenant_ctx.trace_id},
            )
            return RAGResult(
                answer="I don't have enough information to answer this.",
                citations=[],
                retrieval_explain=explain,
                evidence=(),
                gate=gate,
                abstained=True,
                token_usage={},
                query_plan=plan,
            )

        final_ranked = ranked.reranked
        if gate is not None and gate.action == GateAction.RE_RETRIEVE:
            _, retry_fused = await self._retrieve_and_fuse(
                QueryPlan(
                    strategy=plan.strategy,
                    primary=refined,
                    queries=(refined,),
                    sub_queries=plan.sub_queries,
                    used_history=plan.used_history,
                    notes=plan.notes,
                ),
                profile,
                tenant_ctx,
            )
            explain.re_retrieval_attempted = True
            seen = {c.chunk_id for c in ranked.fused}
            added = [c for c in retry_fused if c.chunk_id not in seen]
            explain.re_retrieval_added_chunks = len(added)
            # Validate shared chunk IDs on every second pass, even when the
            # retry contributes no new IDs. Both passes' fused candidates are
            # merged before the single refined-query rerank.
            combined = _interleave_unique(retry_fused, ranked.fused)
            final_ranked = await rerank(
                refined, combined, profile, tenant_ctx, reranker=self.reranker
            )
            explain.rerank_depth = final_ranked.depth
            explain.rerank_model_profile = final_ranked.rerank_model_profile

        selected = select_evidence(final_ranked.ranked, profile)
        parents, parents_resolved = await self._expand(selected, tenant_ctx)
        explain.parents_resolved = parents_resolved
        final = dedupe_overlapping_parents(parents)[: profile.top_k_generate]

        for chunk in final:
            identity = chunk.require_source_identity()
            if identity.tenant_id != tenant_ctx.tenant_id:
                raise ValidationError("evidence tenant does not match the resolved identity")
            if (
                tenant_ctx.allowed_kb_ids is not None
                and identity.kb_id not in tenant_ctx.allowed_kb_ids
            ):
                raise ValidationError("evidence knowledge base is not authorized")
            if chunk.channel == "memory" or chunk.source_text is None:
                raise ValidationError("enterprise evidence requires immutable source text")
            start, end = chunk.start_char_idx, chunk.end_char_idx
            if (
                start is None
                or end is None
                or not 0 <= start < end <= identity.representation_char_count
                or len(chunk.source_text) != end - start
            ):
                raise ValidationError(
                    "enterprise evidence has invalid immutable source coordinates"
                )
            if (
                identity.parent_unit == "page"
                and (chunk.page_no is None or not 1 <= chunk.page_no <= identity.page_count)
            ) or (identity.parent_unit == "token_window" and chunk.page_no is not None):
                raise ValidationError("enterprise evidence has invalid representation pagination")
        evidence = EvidenceSet(chunks=tuple(final))
        answer = await self.generator(refined, final, tenant_ctx)

        citations = bind_markers(answer, evidence)

        # Semantic verification is opt-in online: an unpriced per-claim LLM call
        # on the request path is a cost defect. Structural verification always ran.
        entailment = self.entailment if profile.citation_verify_semantic else None
        citations = await verify_citations(citations, evidence, entailment=entailment)

        supported, generated = citation_counts(citations)
        explain.citations = [c.to_dict() for c in citations]
        explain.citation_supported = supported
        explain.citation_generated = generated

        return RAGResult(
            answer=answer,
            citations=citations,
            retrieval_explain=explain,
            evidence=tuple(final),
            gate=gate,
            abstained=False,
            token_usage={},
            query_plan=plan,
        )

    async def _refine(
        self,
        query: str,
        gate: GateResult | None,
        evidence: list[ScoredChunk],
        tenant_ctx: TenantContext,
    ) -> str:
        if self.refiner is None:
            return deterministic_refine(query)
        return await self.refiner(
            RefineRequest(
                query=query,
                verdict=gate.verdict if gate else None,
                action=gate.action if gate else GateAction.PROCEED,
                evidence=tuple(evidence),
            ),
            tenant_ctx,
        )

    async def _retrieve_and_rerank(
        self,
        plan: QueryPlan,
        profile: RetrieverProfile,
        tenant_ctx: TenantContext,
    ) -> _RecallPass:
        channels, fused = await self._retrieve_and_fuse(plan, profile, tenant_ctx)
        reranked = await rerank(plan.primary, fused, profile, tenant_ctx, reranker=self.reranker)
        return _RecallPass(channels=channels, fused=fused, reranked=reranked)

    async def _retrieve_and_fuse(
        self,
        plan: QueryPlan,
        profile: RetrieverProfile,
        tenant_ctx: TenantContext,
    ) -> tuple[dict[str, list[ScoredChunk]], list[ScoredChunk]]:
        channels = await self._recall(plan, profile, tenant_ctx)
        trimmed, _allocations = allocate_by_budget(channels, profile.channel_budgets)
        fused = self.fusioner.fuse(trimmed, profile.fusion_config)
        return channels, fused

    async def _recall(
        self,
        plan: QueryPlan,
        profile: RetrieverProfile,
        tenant_ctx: TenantContext,
    ) -> dict[str, list[ScoredChunk]]:
        """Run every registered channel against every planned query.

        A decomposed plan therefore recalls N times per channel. Hits are merged
        on ``chunk_id`` keeping the highest score, so a chunk found by two
        sub-queries enters fusion once — otherwise the same passage would occupy
        several slots in the rerank window purely because the question was
        multi-part.
        """
        channels: dict[str, list[ScoredChunk]] = {}
        for name in profile.recall_channels:
            fn = self.recall_channels.get(name)
            if fn is None:
                LOGGER.warning("recall channel not registered; skipping", extra={"channel": name})
                continue
            merged: dict[UUID, ScoredChunk] = {}
            for query in plan.queries:
                for chunk in await fn(query, profile, tenant_ctx):
                    existing = merged.get(chunk.chunk_id)
                    if existing is None or chunk.score > existing.score:
                        merged[chunk.chunk_id] = chunk
            channels[name] = list(merged.values())
        return channels

    async def _expand(
        self, chunks: list[ScoredChunk], tenant_ctx: TenantContext
    ) -> tuple[list[ScoredChunk], int]:
        """Resolve children to their parents.

        The resolver is preferred because it fetches only the parents this
        request actually needs; the injected mapping is the preloaded form used
        when the caller already has the tree. A run with neither expands nothing,
        which the explain reports as ``parents_resolved = 0`` rather than as a
        successful expansion.
        """
        if self.parent_resolver is not None:
            resolved: ResolvedParents = await self.parent_resolver.resolve(chunks, tenant_ctx)
            if resolved.is_empty:
                return chunks, 0
            expanded = expand_to_parents(
                chunks,
                resolved.parents,
                children_by_parent=resolved.children_by_parent or None,
            )
            return expanded, len(resolved.parents)

        if not self.parents:
            return chunks, 0
        expanded = expand_to_parents(
            chunks, self.parents, children_by_parent=self.children_by_parent or None
        )
        return expanded, len(self.parents)


@dataclass(slots=True)
class _RecallPass:
    """One recall → fuse → rerank sweep.

    Returned as a value rather than inlined because the ambiguous gate path
    performs a second sweep with the refined question, and the two sweeps have to
    be combined rather than replacing one another — the first pass may already
    hold chunks the refined query does not surface.
    """

    channels: dict[str, list[ScoredChunk]]
    fused: list[ScoredChunk]
    reranked: RerankResult


def _interleave_unique(
    preferred: list[ScoredChunk], fallback: list[ScoredChunk]
) -> list[ScoredChunk]:
    """Give each query's candidates a fair share of the bounded rerank window.

    Preserve immutable evidence lineage if a chunk appears in both passes.
    """
    merged: list[ScoredChunk] = []
    seen: dict[UUID, ScoredChunk] = {}
    for pair in zip_longest(preferred, fallback):
        for chunk in pair:
            if chunk is None:
                continue
            previous = seen.get(chunk.chunk_id)
            if previous is not None:
                lineage_fields = (
                    "doc_id",
                    "kb_id",
                    "text",
                    "layer",
                    "parent_chunk_id",
                    "page_no",
                    "page_count",
                    "start_char_idx",
                    "end_char_idx",
                    "element_type",
                    "source_identity",
                    "source_text",
                )
                if any(
                    getattr(previous, name) != getattr(chunk, name)
                    for name in lineage_fields
                ):
                    raise ValidationError("cross-pass evidence has conflicting source lineage")
                continue
            seen[chunk.chunk_id] = chunk
            merged.append(chunk)
    return merged


def _as_turns(
    history: list[ConversationTurn] | list[str] | None,
) -> tuple[ConversationTurn, ...] | None:
    """Accept bare strings as user turns.

    A caller holding only the user's own questions should not have to construct
    turn objects, and a bare string in this position is unambiguously a user
    turn — an assistant turn supplied here would be a prompt-injection surface,
    so it is not accepted implicitly.
    """
    if not history:
        return None
    return tuple(
        item
        if isinstance(item, ConversationTurn)
        else ConversationTurn(role="user", text=str(item))
        for item in history
    )
