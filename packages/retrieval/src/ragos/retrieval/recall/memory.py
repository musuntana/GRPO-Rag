"""Memory recall — a parallel channel, never a pre-filter.

AGENTS.md memory rule 7 and the storage rules: memory recall runs *alongside*
knowledge recall and both contribute to fusion. Making memory a sequential
pre-filter would mean the answer is "what the system remembers about the user"
whenever memory has anything to say, which is the opposite of a knowledge base.

Two properties this channel must not lose:

- **actor scope.** Memory rows are per user. ``tenant_ctx.require_user_id()``
  refuses outright when no actor is resolved, because returning the tenant's facts
  to one end user is a disclosure rather than a wider ranking.
- **no budget donation.** An empty memory channel contributes nothing and takes
  nothing from the others; ``allocate_by_budget`` already enforces that, and this
  channel must not compensate by widening its own query when it is empty.
"""

from __future__ import annotations

from ragos.core.tenant import TenantContext
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.storage.repositories.search import SearchRepository, SearchTier


class MemoryRecall:
    """Lexical recall over ``search.memory``, scoped to one actor.

    Vector recall over the memory collection is not wired here; the writer and
    reader would have to agree on an embedding profile, and a channel that half
    exists is worse than one that reports itself as lexical.
    """

    def __init__(self, repo: SearchRepository) -> None:
        self.repo = repo

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        scope_key = tenant_ctx.require_scope_key()
        user_id = tenant_ctx.require_user_id()

        hits = self.repo.search(
            tier=SearchTier.MEMORY,
            query=query,
            scope_key=scope_key,
            limit=profile.top_k_recall,
            user_id=user_id,
        )
        return [
            ScoredChunk(
                chunk_id=hit.chunk_id,
                doc_id=hit.document_id,
                kb_id=hit.kb_id,
                text=hit.body,
                score=hit.rank,
                # A distinct layer so fusion's layer boosts can prefer documented
                # knowledge over recollection without special-casing the channel.
                channel="memory",
                layer="MEM",
                page_no=hit.page_no,
                start_char_idx=hit.start_char_idx,
                end_char_idx=hit.end_char_idx,
                element_type=hit.element_type,
            )
            for hit in hits
        ]
