"""Recover an immutable index plan using independent writes and fenced readback."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan
from ragos.storage.artifacts import ArtifactStore
from ragos.storage.db import bind_session_tenant
from ragos.storage.index_projection import projection_digest
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.kb import (
    ChunkManifestRepository,
    DocumentRepository,
    DocumentVersionRepository,
    require_manifest_contract,
)
from ragos.storage.repositories.search import SearchChunkUpsert, SearchRepository, SearchTier
from sqlmodel import Session

LOGGER = logging.getLogger(__name__)


class VectorIndexWriter(Protocol):
    embedding_profile: str
    embedding_version: str

    def upsert(
        self, *, tenant_ctx: TenantContext, tier: SearchTier, chunks: list[SearchChunkUpsert]
    ) -> None: ...

    def reconcile(
        self, *, tenant_ctx: TenantContext, tier: SearchTier, chunks: list[SearchChunkUpsert]
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class IndexBatchResult:
    manifest_id: UUID
    chunk_total: int
    indexing_state: str


# Fields which may never change while an execution is outside PostgreSQL.
_MANIFEST_FIELDS = (
    "id",
    "tenant_id",
    "kb_id",
    "document_id",
    "document_version_id",
    "representation_id",
    "plan_ref",
    "plan_digest",
    "chunker_version",
    "chunker_config_digest",
    "embedding_profile",
    "embedding_version",
    "parent_unit",
    "page_count",
    "parser_version",
    "chunk_total",
)
_REPRESENTATION_FIELDS = (
    "id",
    "document_version_id",
    "representation_ref",
    "representation_digest",
    "parser_version",
    "render_version",
    "parent_unit",
    "page_count",
    "char_count",
)
_VERSION_FIELDS = ("id", "source_ref", "source_digest", "media_type")


class IngestionService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        tenant_ctx: TenantContext,
        *,
        artifacts: ArtifactStore,
        vector_writer: VectorIndexWriter,
        execution_guard: Callable[[Session], None],
    ) -> None:
        tenant_ctx.require_scope_key()
        if not callable(execution_guard) or vector_writer is None:
            raise ValidationError("index recovery requires a writer and execution fence")
        self.session_factory = session_factory
        self.tenant_ctx = tenant_ctx
        self.artifacts = artifacts
        self.vector_writer = vector_writer
        self.execution_guard = execution_guard

    @contextmanager
    def _transaction(self):
        with self.session_factory() as session:
            bind_session_tenant(session, self.tenant_ctx.tenant_id)
            with session.begin():
                self.execution_guard(session)
                yield session

    def _snapshot(self, session, manifest_id, expected=None):
        repo = ChunkManifestRepository(session, self.tenant_ctx)
        manifest = session.exec(
            repo.scoped_statement()
            .where(repo.model_type.id == manifest_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if manifest is None:
            repo.require_by_id(manifest_id)  # canonical scoped not-found error
            raise ConflictError("Index disappeared")
        representation = require_manifest_contract(session, self.tenant_ctx, manifest)
        version = DocumentVersionRepository(session, self.tenant_ctx).require_by_id(
            manifest.document_version_id
        )
        snapshot = (
            {field: getattr(manifest, field) for field in _MANIFEST_FIELDS},
            {field: getattr(representation, field) for field in _REPRESENTATION_FIELDS},
            {field: getattr(version, field) for field in _VERSION_FIELDS},
        )
        if expected is not None and snapshot != expected:
            raise ConflictError("Frozen index contract changed during execution")
        return manifest, snapshot

    def _checkpoint(self, manifest_id, snapshot):
        with self._transaction() as session:
            self._snapshot(session, manifest_id, snapshot)

    def execute(self, *, manifest_id: UUID) -> IndexBatchResult:
        snapshot = None
        try:
            with self._transaction() as session:
                manifest, snapshot = self._snapshot(session, manifest_id)
                indexed = manifest.indexing_state == "indexed"
                if manifest.indexing_state not in {"pending", "indexing", "failed", "indexed"}:
                    raise ValidationError("Index state cannot be recovered")
            frozen, representation_fields, version_fields = snapshot
            self.artifacts.read_bytes(
                tenant_ctx=self.tenant_ctx,
                reference=version_fields["source_ref"],
                digest=version_fields["source_digest"],
            )
            representation = FrozenRepresentation.model_validate_json(
                self.artifacts.read_bytes(
                    tenant_ctx=self.tenant_ctx,
                    reference=representation_fields["representation_ref"],
                    digest=representation_fields["representation_digest"],
                )
            )
            plan = IndexPlan.model_validate_json(
                self.artifacts.read_bytes(
                    tenant_ctx=self.tenant_ctx,
                    reference=frozen["plan_ref"],
                    digest=frozen["plan_digest"],
                )
            )
            expected_plan = {
                "tenant_id": frozen["tenant_id"],
                "kb_id": frozen["kb_id"],
                "document_id": frozen["document_id"],
                "document_version_id": version_fields["id"],
                "representation_id": representation_fields["id"],
                "index_id": frozen["id"],
                "source_ref": version_fields["source_ref"],
                "source_digest": version_fields["source_digest"],
                "representation_ref": representation_fields["representation_ref"],
                "representation_digest": representation_fields["representation_digest"],
                "chunker_version": frozen["chunker_version"],
                "chunker_config_digest": frozen["chunker_config_digest"],
                "embedding_profile": frozen["embedding_profile"],
                "embedding_version": frozen["embedding_version"],
                "parent_unit": frozen["parent_unit"],
                "representation_char_count": representation_fields["char_count"],
                "page_count": frozen["page_count"],
            }
            if any(getattr(plan, key) != value for key, value in expected_plan.items()):
                raise ValidationError("Stored plan disagrees with authoritative source contract")
            if (representation.parser_version, representation.render_version) != (
                representation_fields["parser_version"],
                representation_fields["render_version"],
            ) or len(plan.chunks) != frozen["chunk_total"]:
                raise ValidationError("Representation profile or planned count does not match")
            if (self.vector_writer.embedding_profile, self.vector_writer.embedding_version) != (
                plan.embedding_profile,
                plan.embedding_version,
            ):
                raise ValidationError(
                    "vector writer does not match the frozen embedding profile/version"
                )
            source_type_by_media = {
                "application/pdf": "pdf",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "text/plain": "plain",
                "text/markdown": "markdown",
                "text/html": "html",
            }
            if source_type_by_media.get(version_fields["media_type"]) != representation.source_type:
                raise ValidationError(
                    "representation source type does not match the original media type"
                )
            plan.verify_representation(representation)
            chunks = plan.as_upserts(self.tenant_ctx)
            expected_digest = plan.projection_digest(self.tenant_ctx)
            tier = SearchTier(plan.tier)
            with self._transaction() as session:
                manifest, _ = self._snapshot(session, manifest_id, snapshot)
                repo = SearchRepository(session, self.tenant_ctx)
                if not indexed:
                    if manifest.indexing_state == "indexed":
                        raise ConflictError("Index was published by another executor")
                    manifest.indexing_state = "indexing"
                    manifest.error_message = None
                    session.add(manifest)
                    session.flush()
                    repo.upsert_chunks(tier=tier, chunks=chunks)
                pg_digest = projection_digest(
                    repo.read_index_for_reconciliation(
                        tier=tier,
                        index_id=manifest_id,
                        scope_key=self.tenant_ctx.require_scope_key(),
                    )
                )
                if pg_digest != expected_digest:
                    raise ConflictError("PostgreSQL readback differs from frozen plan")
                if not indexed:
                    manifest.pg_digest = pg_digest
                    session.add(manifest)
                    session.flush()
            self._checkpoint(manifest_id, snapshot)
            if not indexed:
                self.vector_writer.upsert(tenant_ctx=self.tenant_ctx, tier=tier, chunks=chunks)
            self._checkpoint(manifest_id, snapshot)
            vector_digest = self.vector_writer.reconcile(
                tenant_ctx=self.tenant_ctx,
                tier=tier,
                chunks=chunks,
            )
            if vector_digest != expected_digest:
                raise ConflictError("Vector readback differs from frozen plan")
            with self._transaction() as session:
                manifest, _ = self._snapshot(session, manifest_id, snapshot)
                doc_repo = DocumentRepository(session, self.tenant_ctx)
                if indexed:
                    doc = doc_repo.require_by_id(plan.document_id)
                    if (manifest.pg_digest, manifest.vector_digest, doc.active_index_id) != (
                        expected_digest,
                        expected_digest,
                        manifest_id,
                    ) or manifest.reconciled_at is None:
                        raise ConflictError("Published index readback facts are inconsistent")
                else:
                    if manifest.indexing_state != "indexing":
                        raise ConflictError("Index state changed before publication")
                    # Read PG again in the publication transaction; do not trust cached receipts.
                    digest = projection_digest(
                        SearchRepository(session, self.tenant_ctx).read_index_for_reconciliation(
                            tier=tier,
                            index_id=manifest_id,
                            scope_key=self.tenant_ctx.require_scope_key(),
                        )
                    )
                    if digest != expected_digest:
                        raise ConflictError("PostgreSQL changed before publication")
                    manifest.pg_digest = digest
                    manifest.vector_digest = vector_digest
                    manifest.reconciled_at = utc_now()
                    manifest.indexing_state = "indexed"
                    manifest.updated_at = utc_now()
                    session.add(manifest)
                    session.flush()
                    doc_repo.publish_index(
                        document_id=plan.document_id,
                        index_id=manifest_id,
                        expected_active_index_id=plan.expected_active_index_id,
                        expected_projection_digest=expected_digest,
                    )
            return IndexBatchResult(manifest_id, len(chunks), "indexed")
        except Exception:
            if snapshot is not None:
                try:
                    with self._transaction() as session:
                        manifest, _ = self._snapshot(session, manifest_id, snapshot)
                        if manifest.indexing_state != "indexed":
                            ChunkManifestRepository(session, self.tenant_ctx).mark_failed(
                                manifest_id,
                                error_message="Index recovery failed; inspect trace",
                            )
                except Exception:
                    # A stale executor must never change a successor's state or obscure the cause.
                    LOGGER.warning("index failure could not be recorded under its execution fence")
            raise
