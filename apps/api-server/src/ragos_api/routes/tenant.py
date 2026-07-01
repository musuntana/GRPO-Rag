from __future__ import annotations

from fastapi import APIRouter, Depends
from ragos.auth.deps import require_tenant_admin

from ragos_api.dependencies import TenantCtx

router = APIRouter(
    prefix="/tenant",
    tags=["tenant"],
    dependencies=[Depends(require_tenant_admin)],
)


@router.get("/context")
async def tenant_context(tenant_ctx: TenantCtx) -> dict[str, object]:
    return tenant_ctx.model_dump(mode="json")
