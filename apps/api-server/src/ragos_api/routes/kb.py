from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.core.errors import ValidationError
from ragos.storage.models.app import ChunkManifest, DocumentRecord, KnowledgeBase
from ragos.storage.repositories.kb import (
    DocumentRepository,
    KnowledgeBaseRepository,
)
from ragos.storage.repositories.search import SearchTier

from ragos_api.dependencies import DBSession, TenantCtx

router = APIRouter(
    prefix="/tenant/kb",
    tags=["kb"],
    dependencies=[Depends(require_tenant_admin)],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateKBRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class CreateDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    external_id: str | None = Field(default=None, max_length=255)
    source_uri: str | None = Field(default=None, max_length=1024)


class IngestChunkInput(BaseModel):
    body: str = Field(min_length=1)
    title: str | None = None
    headings_text: str | None = None
    tags_text: str | None = None
    layer: str = Field(default="L1", max_length=16)
    lang: str = Field(default="simple", max_length=16)
    parent_chunk_id: UUID | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentRequest(BaseModel):
    tier: SearchTier = SearchTier.STABLE
    chunks: list[IngestChunkInput] = Field(min_length=1)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def serialize_kb(kb: KnowledgeBase) -> dict[str, Any]:
    return {
        "id": str(kb.id),
        "tenant_id": str(kb.tenant_id),
        "slug": kb.slug,
        "name": kb.name,
        "description": kb.description,
        "created_at": kb.created_at.isoformat(),
        "updated_at": kb.updated_at.isoformat(),
    }


def serialize_document(doc: DocumentRecord) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "tenant_id": str(doc.tenant_id),
        "kb_id": str(doc.kb_id),
        "title": doc.title,
        "external_id": doc.external_id,
        "source_uri": doc.source_uri,
        "ingest_status": doc.ingest_status,
        "active_index_id": str(doc.active_index_id) if doc.active_index_id else None,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
    }


def serialize_manifest(manifest: ChunkManifest) -> dict[str, Any]:
    return {
        "id": str(manifest.id),
        "tenant_id": str(manifest.tenant_id),
        "kb_id": str(manifest.kb_id),
        "document_id": str(manifest.document_id),
        "chunk_total": manifest.chunk_total,
        "indexing_state": manifest.indexing_state,
        "error_message": manifest.error_message,
        "document_version_id": str(manifest.document_version_id)
        if manifest.document_version_id
        else None,
        "representation_id": str(manifest.representation_id)
        if manifest.representation_id
        else None,
    }


# ---------------------------------------------------------------------------
# Knowledge base CRUD
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_kb(
    payload: CreateKBRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    with session.begin():
        kb = KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx).create(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
        )
    return serialize_kb(kb)


@router.get("")
def list_kbs(
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    repo = KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx)
    kbs = repo.list(limit=limit, offset=offset)
    return {"items": [serialize_kb(kb) for kb in kbs]}


@router.get("/{kb_id}")
def get_kb(
    kb_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    kb = KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx).require_by_id(kb_id)
    return serialize_kb(kb)


# ---------------------------------------------------------------------------
# Document CRUD (scoped to a KB)
# ---------------------------------------------------------------------------


@router.post("/{kb_id}/documents", status_code=201)
def create_document(
    kb_id: UUID,
    payload: CreateDocumentRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    with session.begin():
        # Verify the KB belongs to this tenant before attaching a document.
        KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx).require_by_id(kb_id)
        doc = DocumentRepository(session=session, tenant_ctx=tenant_ctx).create(
            kb_id=kb_id,
            title=payload.title,
            external_id=payload.external_id,
            source_uri=payload.source_uri,
        )
    return serialize_document(doc)


@router.get("/{kb_id}/documents")
def list_documents(
    kb_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx).require_by_id(kb_id)
    docs = DocumentRepository(session=session, tenant_ctx=tenant_ctx).list_for_kb(
        kb_id,
        limit=limit,
        offset=offset,
    )
    return {"items": [serialize_document(doc) for doc in docs]}


# ---------------------------------------------------------------------------
# Legacy arbitrary-chunk trigger — explicitly unavailable until versioned preparation
# ---------------------------------------------------------------------------


@router.post("/{kb_id}/documents/{document_id}/ingest", status_code=202)
def ingest_document(
    kb_id: UUID,
    document_id: UUID,
    payload: IngestDocumentRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    # Preserve resource authorization while the old arbitrary-chunk entry is unavailable.
    with session.begin():
        KnowledgeBaseRepository(session=session, tenant_ctx=tenant_ctx).require_by_id(kb_id)
        DocumentRepository(session=session, tenant_ctx=tenant_ctx).require_in_kb(document_id, kb_id)
        raise ValidationError(
            "document ingestion unavailable: immutable source preparation and index recovery "
            "are not connected"
        )
