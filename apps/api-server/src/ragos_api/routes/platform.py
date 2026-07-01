from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_platform_admin
from ragos.storage.models.app import Tenant
from ragos.storage.repositories.platform import PlatformTenantRepository

from ragos_api.dependencies import DBSession

router = APIRouter(
    prefix="/platform/tenants",
    tags=["platform", "tenants"],
    dependencies=[Depends(require_platform_admin)],
)


class CreateTenantRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)


class UpdateTenantRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


def serialize_tenant(tenant: Tenant) -> dict[str, Any]:
    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "display_name": tenant.display_name,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat(),
        "updated_at": tenant.updated_at.isoformat(),
    }


@router.get("")
def list_tenants(
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = PlatformTenantRepository(session)
    tenants = repo.list_all(limit=limit, offset=offset)
    return {"items": [serialize_tenant(t) for t in tenants]}


@router.post("", status_code=201)
def create_tenant(
    payload: CreateTenantRequest,
    session: DBSession,
) -> dict[str, Any]:
    with session.begin():
        tenant = PlatformTenantRepository(session).create(
            slug=payload.slug,
            display_name=payload.display_name,
        )
    return serialize_tenant(tenant)


@router.get("/{tenant_id}")
def get_tenant(tenant_id: UUID, session: DBSession) -> dict[str, Any]:
    tenant = PlatformTenantRepository(session).require_by_id(tenant_id)
    return serialize_tenant(tenant)


@router.patch("/{tenant_id}")
def update_tenant(
    tenant_id: UUID,
    payload: UpdateTenantRequest,
    session: DBSession,
) -> dict[str, Any]:
    with session.begin():
        tenant = PlatformTenantRepository(session).update(
            tenant_id,
            display_name=payload.display_name,
            is_active=payload.is_active,
        )
    return serialize_tenant(tenant)


@router.post("/{tenant_id}/deactivate")
def deactivate_tenant(tenant_id: UUID, session: DBSession) -> dict[str, Any]:
    with session.begin():
        tenant = PlatformTenantRepository(session).update(tenant_id, is_active=False)
    return serialize_tenant(tenant)
