"""Prepare immutable index work without holding PostgreSQL locks during artifact I/O."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan
from ragos.storage.artifacts import ArtifactStore, canonical_json
from ragos.storage.db import bind_session_tenant
from ragos.storage.repositories.kb import (
    ChunkManifestRepository,
    DocumentRepresentationRepository,
    DocumentVersionRepository,
    require_manifest_contract,
)
from ragos.storage.repositories.tasks import MAX_TIMEOUT_SECONDS, TaskRepository


@dataclass(frozen=True, slots=True)
class PreparedIndex:
    manifest_id: UUID
    task_id: UUID
    indexing_state: str


def _semantic_digest(plan: IndexPlan) -> str:
    fields = plan.model_dump(mode="json", exclude={"index_id", "expected_active_index_id"})
    ordinals = {chunk.id: ordinal for ordinal, chunk in enumerate(plan.chunks)}
    for ordinal, chunk in enumerate(fields["chunks"]):
        source = plan.chunks[ordinal]
        chunk["id"] = ordinal
        chunk["parent_chunk_id"] = (
            ordinals[source.parent_chunk_id] if source.parent_chunk_id is not None else None
        )
    return hashlib.sha256(canonical_json(fields)).hexdigest()


_MIME_TYPES = {
    "pdf": {"application/pdf"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "plain": {"text/plain"},
    "markdown": {"text/markdown", "text/plain"},
    "html": {"text/html"},
}


class IndexPreparationService:
    def __init__(self, session_factory, tenant_ctx: TenantContext, artifacts: ArtifactStore):
        tenant_ctx.require_private_scope_key()
        self.session_factory = session_factory
        self.tenant_ctx = tenant_ctx
        self.artifacts = artifacts

    @contextmanager
    def _transaction(self):
        with self.session_factory() as session:
            bind_session_tenant(session, self.tenant_ctx.tenant_id)
            with session.begin():
                yield session

    def _source_contract(self, session, plan):
        version = DocumentVersionRepository(session, self.tenant_ctx).require_in_document(
            version_id=plan.document_version_id, kb_id=plan.kb_id, document_id=plan.document_id
        )
        representation = DocumentRepresentationRepository(
            session, self.tenant_ctx
        ).require_in_version(
            representation_id=plan.representation_id,
            version_id=plan.document_version_id,
            kb_id=plan.kb_id,
            document_id=plan.document_id,
        )
        if (
            version.source_ref,
            version.source_digest,
            representation.representation_ref,
            representation.representation_digest,
            representation.char_count,
            representation.parent_unit,
            representation.page_count,
        ) != (
            plan.source_ref,
            plan.source_digest,
            plan.representation_ref,
            plan.representation_digest,
            plan.representation_char_count,
            plan.parent_unit,
            plan.page_count,
        ):
            raise ConflictError("Plan source contract does not match authoritative records")
        return (version.media_type, representation.parser_version, representation.render_version)

    def _existing_snapshot(self, session, key):
        tasks = TaskRepository(session, self.tenant_ctx)
        tasks.lock_preparation_key(key)
        task = tasks.get_by_idempotency_key(key)
        if task is None:
            return None
        if task.kind != "ingest_document" or set(task.payload) != {"manifest_id"}:
            raise ConflictError("Retry key belongs to another operation")
        try:
            manifest_id = UUID(str(task.payload["manifest_id"]))
        except (ValueError, TypeError) as exc:
            raise ConflictError("Existing preparation has an invalid manifest reference") from exc
        manifest = ChunkManifestRepository(session, self.tenant_ctx).require_by_id(manifest_id)
        require_manifest_contract(session, self.tenant_ctx, manifest)
        return (
            task.id,
            task.timeout_seconds,
            manifest.id,
            manifest.plan_ref,
            manifest.plan_digest,
            {
                field: getattr(manifest, field)
                for field in (
                    "tenant_id",
                    "kb_id",
                    "document_id",
                    "document_version_id",
                    "representation_id",
                    "chunker_version",
                    "chunker_config_digest",
                    "embedding_profile",
                    "embedding_version",
                    "parent_unit",
                    "page_count",
                    "chunk_total",
                )
            },
        )

    def prepare(
        self, *, plan: IndexPlan, idempotency_key: str, timeout_seconds: int = 300
    ) -> PreparedIndex:
        if not isinstance(plan, IndexPlan):
            raise ValidationError("Preparation requires a validated server-created index plan")
        plan.as_upserts(self.tenant_ctx)  # owner/KB validation, never a storage write
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 1024
        ):
            raise ValidationError("A bounded opaque preparation retry key is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not (1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS)
        ):
            raise ValidationError("Preparation timeout must be within the task time budget")
        key = (
            "prepare-index:"
            + hashlib.sha256(
                canonical_json(
                    {
                        "scope": self.tenant_ctx.require_private_scope_key(),
                        "operation": "prepare-index",
                        "retry_key": idempotency_key,
                    }
                )
            ).hexdigest()
        )
        with self._transaction() as session:
            contract = self._source_contract(session, plan)
            existing = self._existing_snapshot(session, key)
        # Every byte read/write happens after the snapshot transaction closes.
        self.artifacts.read_bytes(
            tenant_ctx=self.tenant_ctx, reference=plan.source_ref, digest=plan.source_digest
        )
        representation = FrozenRepresentation.model_validate_json(
            self.artifacts.read_bytes(
                tenant_ctx=self.tenant_ctx,
                reference=plan.representation_ref,
                digest=plan.representation_digest,
            )
        )
        if contract[0] not in _MIME_TYPES[representation.source_type] or contract[1:] != (
            representation.parser_version,
            representation.render_version,
        ):
            raise ValidationError(
                "Frozen representation MIME or parser/render profile does not match"
            )
        plan.verify_representation(representation)
        semantic_digest = _semantic_digest(plan)
        plan_ref = plan_digest = None
        while True:
            if existing is not None:
                old = IndexPlan.model_validate_json(
                    self.artifacts.read_bytes(
                        tenant_ctx=self.tenant_ctx,
                        reference=existing[3],
                        digest=existing[4],
                    )
                )
                if existing[1] != timeout_seconds or _semantic_digest(old) != semantic_digest:
                    raise ConflictError(
                        "Preparation retry key reused with different plan or timeout"
                    )
                if any(
                    (len(old.chunks) if field == "chunk_total" else getattr(old, field)) != value
                    for field, value in existing[5].items()
                ):
                    raise ConflictError("Existing immutable plan does not match manifest contract")
                if old.index_id != existing[2]:
                    raise ConflictError("Existing immutable plan does not match its manifest")
            elif plan_ref is None:
                plan_ref, plan_digest = self.artifacts.put_bytes(
                    tenant_ctx=self.tenant_ctx,
                    kind="index-plan",
                    data=plan.frozen_bytes(),
                )
            with self._transaction() as session:
                if self._source_contract(session, plan) != contract:
                    raise ConflictError("Source contract changed while preparing artifacts")
                current = self._existing_snapshot(session, key)
                if existing is not None:
                    if current != existing:
                        raise ConflictError("Preparation identity changed during retry validation")
                    manifest = ChunkManifestRepository(session, self.tenant_ctx).require_by_id(
                        existing[2]
                    )
                    return PreparedIndex(manifest.id, existing[0], manifest.indexing_state)
                if current is None:
                    manifest = ChunkManifestRepository(session, self.tenant_ctx).create(
                        index_id=plan.index_id,
                        kb_id=plan.kb_id,
                        document_id=plan.document_id,
                        document_version_id=plan.document_version_id,
                        representation_id=plan.representation_id,
                        chunker_version=plan.chunker_version,
                        chunker_config_digest=plan.chunker_config_digest,
                        embedding_profile=plan.embedding_profile,
                        embedding_version=plan.embedding_version,
                        plan_ref=plan_ref,
                        plan_digest=plan_digest,
                        chunk_total=len(plan.chunks),
                    )
                    task = TaskRepository(session, self.tenant_ctx).enqueue(
                        kind="ingest_document",
                        payload={"manifest_id": str(manifest.id)},
                        idempotency_key=key,
                        timeout_seconds=timeout_seconds,
                    )
                    return PreparedIndex(manifest.id, task.id, manifest.indexing_state)
                # A concurrent preparation won. Validate its plan outside the lock.
                existing = current
