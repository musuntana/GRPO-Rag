"""Lexical recall.

PostgreSQL ``ts_rank_cd``, not BM25 — the design chose proximity-aware ranking
deliberately, so the module is named for what it does rather than for the
algorithm it resembles. Dense and hot recall live in their own modules.

``scope_key`` is passed explicitly, never read from ambient state, so a caller
cannot widen the query by reaching into the context.
"""

from __future__ import annotations

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.storage.repositories.search import SearchRepository, SearchTier

LOGGER = get_logger(__name__)


class LexicalRecall:
    """PostgreSQL full-text recall over the ``search`` schema."""

    def __init__(self, repo: SearchRepository, tier: SearchTier = SearchTier.STABLE) -> None:
        self.repo = repo
        self.tier = tier

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        scope_key = tenant_ctx.require_scope_key()
        kb_ids = tenant_ctx.allowed_kb_ids
        hits = self.repo.search(
            tier=self.tier,
            query=query,
            scope_key=scope_key,
            limit=profile.top_k_recall,
            kb_ids=kb_ids,
        )
        return [
            ScoredChunk(
                chunk_id=hit.chunk_id,
                doc_id=hit.document_id,
                kb_id=hit.kb_id,
                text=hit.body,
                score=hit.rank,
                channel="lexical",
                layer=hit.layer,
                page_no=hit.page_no,
                page_count=hit.page_count,
                start_char_idx=hit.start_char_idx,
                end_char_idx=hit.end_char_idx,
                element_type=hit.element_type,
            )
            for hit in hits
        ]


class UnimplementedRecall:
    """Placeholder for a channel a profile names but nothing serves.

    Returning empty rather than raising keeps one missing channel from taking
    down retrieval; ``RetrievalService`` reports the gap in the response so the
    shortfall is visible rather than inferred from a quality number.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        LOGGER.warning(
            "recall channel registered but not implemented; contributing nothing",
            extra={"channel": self.name, "trace_id": tenant_ctx.trace_id},
        )
        return []
