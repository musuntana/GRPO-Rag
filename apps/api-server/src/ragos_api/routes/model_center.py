from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_platform_admin
from ragos.storage.repositories.model_gateway import (
    KeyPoolRepository,
    PlatformModelProfileRepository,
    ProviderRepository,
)

from ragos_api.dependencies import DBSession

router = APIRouter(
    prefix="/platform/model-center",
    tags=["platform", "model-center"],
    dependencies=[Depends(require_platform_admin)],
)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def serialize_provider(p) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "slug": p.slug,
        "provider_type": p.provider_type,
        "display_name": p.display_name,
        "base_url": p.base_url,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
    }


def serialize_key(k) -> dict[str, Any]:
    return {
        "id": str(k.id),
        "provider_id": str(k.provider_id),
        "label": k.label,
        "weight": k.weight,
        "is_active": k.is_active,
        "consecutive_failures": k.consecutive_failures,
        "created_at": k.created_at.isoformat(),
    }


def serialize_profile(p) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "tenant_id": str(p.tenant_id),
        "name": p.name,
        "use_case": p.use_case,
        "provider_id": str(p.provider_id),
        "model_name": p.model_name,
        "parameters": p.parameters,
        "fallback_profile_id": str(p.fallback_profile_id) if p.fallback_profile_id else None,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class CreateProviderRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    provider_type: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=512)


@router.get("/providers")
def list_providers(
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = ProviderRepository(session)
    providers = repo.list(limit=limit, offset=offset)
    return {"items": [serialize_provider(p) for p in providers]}


@router.post("/providers", status_code=201)
def create_provider(payload: CreateProviderRequest, session: DBSession) -> dict[str, Any]:
    with session.begin():
        provider = ProviderRepository(session).create(
            slug=payload.slug,
            provider_type=payload.provider_type,
            display_name=payload.display_name,
            base_url=payload.base_url,
        )
    return serialize_provider(provider)


@router.get("/providers/{provider_id}")
def get_provider(provider_id: UUID, session: DBSession) -> dict[str, Any]:
    return serialize_provider(ProviderRepository(session).require_by_id(provider_id))


# ---------------------------------------------------------------------------
# Key Pools
# ---------------------------------------------------------------------------


class CreateKeyPoolRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=512)
    weight: int = Field(default=1, ge=1)


@router.get("/providers/{provider_id}/key-pools")
def list_key_pools(
    provider_id: UUID,
    session: DBSession,
) -> dict[str, Any]:
    repo = KeyPoolRepository(session)
    keys = repo.list_for_provider(provider_id)
    return {"items": [serialize_key(k) for k in keys]}


@router.post("/providers/{provider_id}/key-pools", status_code=201)
def create_key_pool(
    provider_id: UUID,
    payload: CreateKeyPoolRequest,
    session: DBSession,
) -> dict[str, Any]:
    with session.begin():
        key = KeyPoolRepository(session).create(
            provider_id=provider_id,
            label=payload.label,
            api_key=payload.api_key,
            weight=payload.weight,
        )
    return serialize_key(key)


# ---------------------------------------------------------------------------
# Model Profiles
# ---------------------------------------------------------------------------


class CreateProfileRequest(BaseModel):
    tenant_id: UUID
    name: str = Field(min_length=1, max_length=255)
    use_case: str = Field(min_length=1, max_length=32)
    provider_id: UUID
    model_name: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback_profile_id: UUID | None = None


@router.post("/profiles", status_code=201)
def create_profile(payload: CreateProfileRequest, session: DBSession) -> dict[str, Any]:
    with session.begin():
        profile = PlatformModelProfileRepository(session).create(
            tenant_id=payload.tenant_id,
            name=payload.name,
            use_case=payload.use_case,
            provider_id=payload.provider_id,
            model_name=payload.model_name,
            parameters=payload.parameters,
            fallback_profile_id=payload.fallback_profile_id,
        )
    return serialize_profile(profile)


@router.get("/profiles")
def list_profiles(
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant_id: UUID | None = None,
    use_case: str | None = None,
) -> dict[str, Any]:
    repo = PlatformModelProfileRepository(session)
    profiles = repo.list(limit=limit, offset=offset, tenant_id=tenant_id)
    if use_case:
        profiles = [p for p in profiles if p.use_case == use_case]
    return {"items": [serialize_profile(p) for p in profiles]}


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: UUID, session: DBSession) -> dict[str, Any]:
    return serialize_profile(PlatformModelProfileRepository(session).require_by_id(profile_id))
