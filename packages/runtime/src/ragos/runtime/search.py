from __future__ import annotations

from uuid import UUID

from ragos.core.tenant import TenantContext
from ragos.storage.repositories.search import SearchHit, SearchRepository, SearchTier
from sqlmodel import Session


class SearchService:
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        self.repo = SearchRepository(session=session, tenant_ctx=tenant_ctx)

    def query(
        self,
        *,
        tier: SearchTier,
        query: str,
        limit: int = 10,
        kb_ids: list[UUID] | None = None,
        layer: str | None = None,
    ) -> list[SearchHit]:
        return self.repo.search(
            scope_key=(
                self.repo.tenant_ctx.require_private_scope_key()
                if tier == SearchTier.MEMORY
                else self.repo.tenant_ctx.require_scope_key()
            ),
            tier=tier,
            query=query,
            limit=limit,
            kb_ids=kb_ids,
            layer=layer,
        )
