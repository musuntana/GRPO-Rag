"""Hot-tier (L3) recall.

L3 is physically separate from stable knowledge on purpose: it is short-lived,
high-churn content — announcements, incident notes, temporary policy overrides —
and mixing it into the stable tier makes the stable tier's ranking depend on how
much of the recent past a tenant happens to have published.

The freshness signal lives in fusion, not here. This channel's only job is to
return ranked hot chunks under the same tenant scope as everything else.
"""

from __future__ import annotations

from ragos.core.tenant import TenantContext
from ragos.retrieval.identity import identity_from_hit
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.storage.repositories.search import SearchRepository, SearchTier


class HotTierRecall:
    """L3 hot-tier recall, scored by ``ts_rank_cd`` over ``search.chunk_hot``."""

    def __init__(self, repo: SearchRepository) -> None:
        self.repo = repo

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        scope_key = tenant_ctx.require_scope_key()
        hits = self.repo.search(
            tier=SearchTier.HOT,
            query=query,
            scope_key=scope_key,
            limit=profile.top_k_recall,
            kb_ids=tenant_ctx.allowed_kb_ids,
        )
        return [
            ScoredChunk(
                chunk_id=hit.chunk_id,
                doc_id=hit.document_id,
                kb_id=hit.kb_id,
                text=hit.body,
                score=hit.rank,
                channel="hot",
                layer="L3",
                page_no=hit.page_no,
                page_count=hit.page_count,
                start_char_idx=hit.start_char_idx,
                end_char_idx=hit.end_char_idx,
                element_type=hit.element_type,
                source_identity=identity_from_hit(hit, tenant_id=tenant_ctx.tenant_id),
                source_text=hit.body,
            )
            for hit in hits
        ]
