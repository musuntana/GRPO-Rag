"""Two-stage retrieval: recall for recall, rerank for precision.

The division of labour is the whole design:

    retriever  -> optimize recall  (oversample; never drop the answer early)
    reranker   -> optimize precision (a smaller set, scored precisely)

A candidate dropped by the retriever can never be recovered by anything
downstream, which is why recall is measured separately (recall@k) rather than
inferred from ranking quality.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import RetrieverProfile, ScoredChunk

#: Signature of a rerank call. Always reached through the model gateway — a
#: trained reranker included (ADR-0007), so there is no code path from a
#: retrieval feature to a model that bypasses the gateway.
RerankFn = Callable[[str, list[ScoredChunk], TenantContext], Awaitable[list[ScoredChunk]]]


@dataclass(slots=True)
class RerankResult:
    query: str
    ranked: list[ScoredChunk]
    rerank_model_profile: str | None
    depth: int


class IdentityReranker:
    """Pass-through reranker.

    The correct default when no rerank model is configured: the pipeline still
    runs, still measures, and the retrieval metrics say whether adding one
    would help. A reranker that always runs but is never evaluated is worse
    than none, because it costs latency invisibly.
    """

    async def rerank(
        self,
        query: str,
        chunks: list[ScoredChunk],
        tenant_ctx: TenantContext,
    ) -> list[ScoredChunk]:
        return chunks


class GatewayReranker:
    """Rerank through the model gateway.

    ``profile_id`` names the ``model_profile``; the gateway resolves it, checks
    entitlement and remaining budget, and records the resolved provider. No
    provider call is made from here.
    """

    def __init__(self, profile_id: str, call: RerankFn) -> None:
        self.profile_id = profile_id
        self.call = call

    async def rerank(
        self,
        query: str,
        chunks: list[ScoredChunk],
        tenant_ctx: TenantContext,
    ) -> list[ScoredChunk]:
        return await self.call(query, chunks, tenant_ctx)


async def rerank(
    query: str,
    chunks: list[ScoredChunk],
    profile: RetrieverProfile,
    tenant_ctx: TenantContext,
    *,
    reranker: RerankFn | None = None,
) -> RerankResult:
    """Rerank at most ``top_k_rerank`` of the fused candidates.

    Depth is recorded because rerank depth is an evaluated parameter, not a
    constant chosen by intuition.
    """
    depth = min(profile.top_k_rerank, len(chunks))
    head = list(chunks)[:depth]

    if reranker is None or profile.rerank_model_profile is None:
        return RerankResult(
            query=query,
            ranked=head,
            rerank_model_profile=None,
            depth=depth,
        )

    ranked = await reranker(query, head, tenant_ctx)
    originals = {c.chunk_id: c for c in head}
    for candidate in ranked:
        original = originals.get(candidate.chunk_id)
        if original is None or (
            candidate.source_identity,
            candidate.source_text,
            candidate.page_no,
            candidate.start_char_idx,
            candidate.end_char_idx,
        ) != (
            original.source_identity,
            original.source_text,
            original.page_no,
            original.start_char_idx,
            original.end_char_idx,
        ):
            raise ValidationError("reranker changed immutable evidence lineage")
    marked = [
        ScoredChunk(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            kb_id=c.kb_id,
            text=c.text,
            score=c.score,
            channel=c.channel,
            layer=c.layer,
            parent_chunk_id=c.parent_chunk_id,
            page_no=c.page_no,
            page_count=c.page_count,
            start_char_idx=c.start_char_idx,
            end_char_idx=c.end_char_idx,
            element_type=c.element_type,
            source_identity=c.source_identity,
            source_text=c.source_text,
            rerank_score=c.score,
        )
        for c in ranked
    ]
    return RerankResult(
        query=query,
        ranked=marked,
        rerank_model_profile=profile.rerank_model_profile,
        depth=depth,
    )


def select_evidence(
    chunks: list[ScoredChunk],
    profile: RetrieverProfile,
) -> list[ScoredChunk]:
    """Take the top K that reaches generation.

    Fewer is not automatically better, and more is not free: irrelevant context
    causes attention dilution, contradictory evidence, and lost-in-the-middle.
    ``top_k_generate`` is a profile value with a recorded evaluation behind it.
    """
    return list(chunks)[: profile.top_k_generate]


def evidence_page_span(chunks: list[ScoredChunk]) -> tuple[int | None, int | None]:
    pages = [c.page_no for c in chunks if c.page_no is not None]
    if not pages:
        return None, None
    return min(pages), max(pages)


def channel_of(chunks: list[ScoredChunk], channel: str) -> list[ScoredChunk]:
    return [c for c in chunks if c.channel == channel]


def chunk_ids(chunks: list[ScoredChunk]) -> set[UUID]:
    return {c.chunk_id for c in chunks}
