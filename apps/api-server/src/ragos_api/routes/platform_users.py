from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_platform_admin
from ragos.core.errors import NotFoundError
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.users import PlatformUserRepository

from ragos_api.dependencies import DBSession

router = APIRouter(
    prefix="/platform/tenants/{tenant_id}/users",
    tags=["platform", "users"],
    dependencies=[Depends(require_platform_admin)],
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


@router.get("")
def list_users(
    tenant_id: UUID,
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = PlatformUserRepository(session)
    users = repo.list_for_tenant(tenant_id, limit=limit, offset=offset)
    return {"items": [serialize_user(u) for u in users]}


@router.post("", status_code=201)
def create_user(
    tenant_id: UUID,
    payload: CreateUserRequest,
    session: DBSession,
) -> dict[str, Any]:
    with session.begin():
        user = PlatformUserRepository(session).create(
            tenant_id=tenant_id,
            external_id=payload.external_id,
            display_name=payload.display_name,
            email=payload.email,
            roles=payload.roles,
        )
    return serialize_user(user)


@router.get("/{user_id}")
def get_user(tenant_id: UUID, user_id: UUID, session: DBSession) -> dict[str, Any]:
    user = PlatformUserRepository(session).require_by_id(user_id)
    if user.tenant_id != tenant_id:
        raise NotFoundError(
            "User not found in this tenant",
            resource="User",
            id=str(user_id),
        )
    return serialize_user(user)


@router.patch("/{user_id}")
def update_user(
    tenant_id: UUID,
    user_id: UUID,
    payload: UpdateUserRequest,
    session: DBSession,
) -> dict[str, Any]:
    repo = PlatformUserRepository(session)
    user = repo.require_by_id(user_id)
    if user.tenant_id != tenant_id:
        raise NotFoundError(
            "User not found in this tenant",
            resource="User",
            id=str(user_id),
        )
    with session.begin():
        if payload.display_name is not None:
            user.display_name = payload.display_name
        if payload.email is not None:
            user.email = payload.email
        if payload.roles is not None:
            user.roles = payload.roles
        if payload.is_active is not None:
            user.is_active = payload.is_active
        user.updated_at = utc_now()
        repo.add(user)
    return serialize_user(user)


@router.delete("/{user_id}", status_code=204)
def deactivate_user(tenant_id: UUID, user_id: UUID, session: DBSession) -> None:
    repo = PlatformUserRepository(session)
    user = repo.require_by_id(user_id)
    if user.tenant_id != tenant_id:
        raise NotFoundError(
            "User not found in this tenant",
            resource="User",
            id=str(user_id),
        )
    with session.begin():
        user.is_active = False
        user.updated_at = utc_now()
        repo.add(user)
