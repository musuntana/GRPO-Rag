from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.runtime.search import SearchService
from ragos.storage.repositories.search import SearchTier

from ragos_api.dependencies import DBSession, TenantCtx

router = APIRouter(
    prefix="/tenant/search",
    tags=["search"],
    dependencies=[Depends(require_tenant_admin)],
)


class SearchRequest(BaseModel):
    query: str
    tier: SearchTier = SearchTier.STABLE
    limit: int = Field(default=10, ge=1, le=50)
    kb_ids: list[UUID] | None = None
    layer: str | None = None


@router.post("/query")
def query_search(
    payload: SearchRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    service = SearchService(session=session, tenant_ctx=tenant_ctx)
    hits = service.query(
        tier=payload.tier,
        query=payload.query,
        limit=payload.limit,
        kb_ids=payload.kb_ids,
        layer=payload.layer,
    )
    return {
        "hits": [
            {
                "chunk_id": str(hit.chunk_id),
                "document_id": str(hit.document_id) if hit.document_id else None,
                "kb_id": str(hit.kb_id) if hit.kb_id else None,
                "layer": hit.layer,
                "body": hit.body,
                "metadata_json": hit.metadata_json,
                "rank": hit.rank,
            }
            for hit in hits
        ]
    }
