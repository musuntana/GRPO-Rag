"""Retriever profile versions — the tenant-facing configuration of the read path.

Profiles are append-only. Every write creates a new version row rather than
editing one, because an eval result names the version it ran against: a mutable
profile makes a stored metric a report of a configuration that no longer exists.

Configuration is validated before it is stored. A row that cannot produce a valid
runtime profile is a row that would fail on the request path, so it is rejected
here with the same error the request path would have raised.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.core.tenant import TenantContext
from ragos.retrieval.profile import profile_from_row
from ragos.retrieval.types import DEFAULT_CHANNEL_BUDGETS
from ragos.storage.models.app import RetrieverProfileVersion
from ragos.storage.repositories.retriever import RetrieverProfileRepository

from ragos_api.dependencies import DBSession, TenantCtx
from ragos_api.scope import discard_scope_fields, enforce_body_scope

router = APIRouter(
    prefix="/tenant/retriever-profiles",
    tags=["retrieval"],
    dependencies=[Depends(require_tenant_admin), Depends(enforce_body_scope)],
)


class ProfileConfig(BaseModel):
    """The behaviour-carrying fields of a version."""

    query_strategy: str = "direct"
    recall_channels: list[str] = Field(default_factory=lambda: ["lexical"])
    join_mode: str = "reciprocal_rank_fusion"
    channel_budgets: dict[str, float] = Field(default_factory=dict)
    fusion_config: dict[str, Any] = Field(default_factory=dict)
    crag_thresholds: dict[str, float] = Field(default_factory=dict)
    crag_enabled: bool = True
    rerank_model_profile: str | None = None
    citation_verify_semantic: bool = False
    top_k_recall: int = Field(default=50, ge=1, le=500)
    top_k_rerank: int = Field(default=20, ge=1, le=200)
    top_k_generate: int = Field(default=6, ge=1, le=50)


class CreateProfileRequest(ProfileConfig):
    profile_id: UUID | None = None
    name: str | None = Field(default=None, max_length=128)


class CreateVersionRequest(ProfileConfig):
    """Every field is optional on a new version: an omitted field inherits the
    previous version's value, so a one-line tuning change does not have to
    restate the whole configuration and risk diverging from it."""

    name: str | None = Field(default=None, max_length=128)


def _row_fields(config: ProfileConfig) -> dict[str, Any]:
    return {
        "query_strategy": config.query_strategy,
        "recall_channels": list(config.recall_channels),
        "join_mode": config.join_mode,
        "channel_budgets": dict(config.channel_budgets),
        "fusion_config": dict(config.fusion_config),
        "crag_thresholds": dict(config.crag_thresholds),
        "crag_enabled": config.crag_enabled,
        "rerank_model_profile": config.rerank_model_profile,
        "citation_verify_semantic": config.citation_verify_semantic,
        "top_k_recall": config.top_k_recall,
        "top_k_rerank": config.top_k_rerank,
        "top_k_generate": config.top_k_generate,
    }


def _validate(row: RetrieverProfileVersion) -> None:
    """Reject a configuration the read path could not honour.

    Called before insert. The alternative is storing a row that raises
    ``VALIDATION_ERROR`` on every retrieval request, which reads to the tenant as
    "the API is broken" rather than "this configuration is invalid".
    """
    profile_from_row(row)


def _serialize(row: RetrieverProfileVersion) -> dict[str, Any]:
    return {
        "profile_id": str(row.profile_id),
        "version": row.version,
        "name": row.name,
        "query_strategy": row.query_strategy,
        "recall_channels": list(row.recall_channels),
        "join_mode": row.join_mode,
        "channel_budgets": dict(row.channel_budgets),
        "fusion_config": dict(row.fusion_config),
        "crag_thresholds": dict(row.crag_thresholds),
        "crag_enabled": row.crag_enabled,
        "rerank_model_profile": row.rerank_model_profile,
        "citation_verify_semantic": row.citation_verify_semantic,
        "top_k_recall": row.top_k_recall,
        "top_k_rerank": row.top_k_rerank,
        "top_k_generate": row.top_k_generate,
    }


def _repo(session: DBSession, tenant_ctx: TenantContext) -> RetrieverProfileRepository:
    return RetrieverProfileRepository(session=session, tenant_ctx=tenant_ctx)


@router.get("")
def list_profiles(
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = _repo(session, tenant_ctx)
    rows = repo.list_profiles(scope_key=tenant_ctx.require_scope_key(), limit=limit, offset=offset)
    return {"items": [_serialize(row) for row in rows], "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_profile(
    payload: CreateProfileRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    discard_scope_fields(
        payload.model_dump(exclude_none=True), tenant_ctx, where="retriever_profile"
    )

    repo = _repo(session, tenant_ctx)
    scope_key = tenant_ctx.require_scope_key()
    fields = _row_fields(payload)
    if not payload.channel_budgets:
        fields["channel_budgets"] = {
            name: DEFAULT_CHANNEL_BUDGETS.get(name, 0.0)
            for name in payload.recall_channels
        }

    row = repo.create_version(
        scope_key=scope_key,
        profile_id=payload.profile_id or uuid4(),
        name=payload.name,
        **fields,
    )
    _validate(row)
    session.commit()
    return _serialize(row)


@router.get("/{profile_id}/versions")
def list_versions(
    profile_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    repo = _repo(session, tenant_ctx)
    rows = repo.list_versions(scope_key=tenant_ctx.require_scope_key(), profile_id=profile_id)
    return {"items": [_serialize(row) for row in rows]}


@router.get("/{profile_id}")
def get_profile(
    profile_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
    version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    repo = _repo(session, tenant_ctx)
    scope_key = tenant_ctx.require_scope_key()
    if version is None:
        row = repo.require_latest_version(scope_key=scope_key, profile_id=profile_id)
    else:
        row = repo.require_version(scope_key=scope_key, profile_id=profile_id, version=version)
    return _serialize(row)


@router.post("/{profile_id}/versions", status_code=201)
def create_version(
    profile_id: UUID,
    payload: CreateVersionRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    """Append a version, inheriting every field the request leaves unset."""
    discard_scope_fields(
        payload.model_dump(exclude_none=True), tenant_ctx, where="retriever_profile"
    )

    repo = _repo(session, tenant_ctx)
    scope_key = tenant_ctx.require_scope_key()
    previous = repo.require_latest_version(scope_key=scope_key, profile_id=profile_id)

    provided = payload.model_dump(exclude_unset=True)
    # An explicit ``null`` clears a field (that is how a tenant turns off a
    # rerank model); an omitted field inherits. ``exclude_unset`` is what
    # separates the two — with ``exclude_none`` a client could not clear
    # anything, only add.
    inherited: dict[str, Any] = {"name": previous.name, **_row_fields_from_row(previous)}
    inherited.update(provided)

    row = repo.create_version(scope_key=scope_key, profile_id=profile_id, **inherited)
    _validate(row)
    session.commit()
    return _serialize(row)


def _row_fields_from_row(row: RetrieverProfileVersion) -> dict[str, Any]:
    return {
        "query_strategy": row.query_strategy,
        "recall_channels": list(row.recall_channels),
        "join_mode": row.join_mode,
        "channel_budgets": dict(row.channel_budgets),
        "fusion_config": dict(row.fusion_config),
        "crag_thresholds": dict(row.crag_thresholds),
        "crag_enabled": row.crag_enabled,
        "rerank_model_profile": row.rerank_model_profile,
        "citation_verify_semantic": row.citation_verify_semantic,
        "top_k_recall": row.top_k_recall,
        "top_k_rerank": row.top_k_rerank,
        "top_k_generate": row.top_k_generate,
    }
