from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.storage.repositories.model_gateway import TenantModelBindingRepository

from ragos_api.dependencies import DBSession, TenantCtx

router = APIRouter(
    prefix="/tenant/model-bindings",
    tags=["model-bindings"],
    dependencies=[Depends(require_tenant_admin)],
)


class UpsertBindingRequest(BaseModel):
    use_case: str = Field(min_length=1, max_length=32)
    profile_id: UUID
    is_default: bool = False


def serialize_binding(b) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "tenant_id": str(b.tenant_id),
        "use_case": b.use_case,
        "profile_id": str(b.profile_id),
        "is_default": b.is_default,
        "created_at": b.created_at.isoformat(),
        "updated_at": b.updated_at.isoformat(),
    }


@router.put("")
def upsert_binding(
    payload: UpsertBindingRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    with session.begin():
        binding = TenantModelBindingRepository(
            session=session, tenant_ctx=tenant_ctx
        ).upsert(
            use_case=payload.use_case,
            profile_id=payload.profile_id,
            is_default=payload.is_default,
        )
    return serialize_binding(binding)


@router.get("")
def list_bindings(
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    repo = TenantModelBindingRepository(session=session, tenant_ctx=tenant_ctx)
    bindings = repo.list()
    return {"items": [serialize_binding(b) for b in bindings]}


@router.get("/{binding_id}")
def get_binding(
    binding_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    binding = TenantModelBindingRepository(
        session=session, tenant_ctx=tenant_ctx
    ).require_by_id(binding_id)
    return serialize_binding(binding)


@router.delete("/{binding_id}", status_code=204)
def delete_binding(
    binding_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> None:
    with session.begin():
        repo = TenantModelBindingRepository(session=session, tenant_ctx=tenant_ctx)
        binding = repo.require_by_id(binding_id)
        repo.session.delete(binding)
