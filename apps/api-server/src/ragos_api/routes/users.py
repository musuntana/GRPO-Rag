from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.storage.repositories.users import UserRepository

from ragos_api.dependencies import DBSession, TenantCtx

router = APIRouter(
    prefix="/tenant/users",
    tags=["users"],
    dependencies=[Depends(require_tenant_admin)],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    roles: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    roles: list[str] | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


def serialize_user(user) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "tenant_id": str(user.tenant_id),
        "external_id": user.external_id,
        "display_name": user.display_name,
        "email": user.email,
        "roles": user.roles,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_user(
    payload: CreateUserRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    with session.begin():
        user = UserRepository(session=session, tenant_ctx=tenant_ctx).create(
            external_id=payload.external_id,
            display_name=payload.display_name,
            email=payload.email,
            roles=payload.roles,
        )
    return serialize_user(user)


@router.get("")
def list_users(
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = UserRepository(session=session, tenant_ctx=tenant_ctx)
    users = repo.list(limit=limit, offset=offset)
    return {"items": [serialize_user(u) for u in users]}


@router.get("/{user_id}")
def get_user(
    user_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    user = UserRepository(session=session, tenant_ctx=tenant_ctx).require_by_id(user_id)
    return serialize_user(user)


@router.patch("/{user_id}")
def update_user(
    user_id: UUID,
    payload: UpdateUserRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    with session.begin():
        user = UserRepository(session=session, tenant_ctx=tenant_ctx).update(
            user_id,
            display_name=payload.display_name,
            email=payload.email,
            roles=payload.roles,
            is_active=payload.is_active,
        )
    return serialize_user(user)


@router.delete("/{user_id}", status_code=204)
def deactivate_user(
    user_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> None:
    with session.begin():
        UserRepository(session=session, tenant_ctx=tenant_ctx).update(
            user_id,
            is_active=False,
        )
