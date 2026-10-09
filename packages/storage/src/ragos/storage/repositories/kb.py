from __future__ import annotations

import re
from uuid import UUID

from ragos.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.artifacts import require_artifact_ref
from ragos.storage.models.app import (
    ChunkManifest,
    DocumentRecord,
    DocumentRepresentation,
    DocumentVersion,
    KnowledgeBase,
)
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

_DIGEST = re.compile(r"[0-9a-f]{64}")


def require_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValidationError("a lowercase SHA-256 digest is required")


def require_profile(value: str, *, name: str, max_length: int = 128) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ValidationError(f"{name} must identify a frozen configuration")


def require_kb(session: Session, tenant_ctx: TenantContext, kb_id: UUID) -> KnowledgeBase:
    tenant_ctx.require_scope_key()
    if tenant_ctx.allowed_kb_ids is not None and kb_id not in tenant_ctx.allowed_kb_ids:
        raise PermissionDeniedError("knowledge base is not authorized")
    return TenantRepository(session, tenant_ctx, KnowledgeBase).require_by_id(kb_id)


def require_pagination(*, parent_unit: str, char_count: int, page_count: int | None) -> None:
    if isinstance(char_count, bool) or not isinstance(char_count, int) or char_count < 0:
        raise ValidationError("representation character count must be nonnegative")
    if parent_unit not in {"page", "token_window"}:
        raise ValidationError("unknown representation parent unit")
    if parent_unit == "page":
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            raise ValidationError("paginated representation requires a positive page count")
    elif page_count is not None:
        raise ValidationError("token-window representation cannot claim page navigation")


class KnowledgeBaseRepository(TenantRepository[KnowledgeBase]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=KnowledgeBase)

    def get_by_slug(self, slug: str) -> KnowledgeBase | None:
        stmt = self.scoped_statement().where(KnowledgeBase.slug == slug)
        return self.session.exec(stmt).first()

    def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None = None,
    ) -> KnowledgeBase:
        if self.get_by_slug(slug) is not None:
            raise ConflictError(
                "Knowledge base slug already exists",
                slug=slug,
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        kb = KnowledgeBase(
            tenant_id=self.tenant_ctx.tenant_id,
            slug=slug,
            name=name,
            description=description,
        )
        self.session.add(kb)
        self.session.flush()
        self.session.refresh(kb)
        return kb


class DocumentRepository(TenantRepository[DocumentRecord]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=DocumentRecord)

    def scoped_statement(self):
        statement = super().scoped_statement()
        if self.tenant_ctx.allowed_kb_ids is not None:
            statement = statement.where(DocumentRecord.kb_id.in_(self.tenant_ctx.allowed_kb_ids))
        return statement

    def list_for_kb(
        self,
        kb_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        require_kb(self.session, self.tenant_ctx, kb_id)
        stmt = (
            self.scoped_statement().where(DocumentRecord.kb_id == kb_id).offset(offset).limit(limit)
        )
        return list(self.session.exec(stmt).all())

    def create(
        self,
        *,
        kb_id: UUID,
        title: str,
        external_id: str | None = None,
        source_uri: str | None = None,
    ) -> DocumentRecord:
        require_kb(self.session, self.tenant_ctx, kb_id)
        doc = DocumentRecord(
            tenant_id=self.tenant_ctx.tenant_id,
            kb_id=kb_id,
            title=title,
            external_id=external_id,
            source_uri=source_uri,
        )
        self.session.add(doc)
        self.session.flush()
        self.session.refresh(doc)
        return doc

    def require_in_kb(self, document_id: UUID, kb_id: UUID) -> DocumentRecord:
        require_kb(self.session, self.tenant_ctx, kb_id)
        doc = self.require_by_id(document_id)
        if doc.kb_id != kb_id:
            raise NotFoundError(
                "Document does not belong to the given knowledge base",
                resource="DocumentRecord",
                id=str(document_id),
                kb_id=str(kb_id),
            )
        return doc

    def publish_index(
        self,
        *,
        document_id: UUID,
        index_id: UUID,
        expected_active_index_id: UUID | None,
        expected_projection_digest: str,
    ) -> DocumentRecord:
        # Caller owns the transaction. Manifest lock serializes staging writes,
        # reconciliation and publication; CAS never changes another document.
        manifest = self.session.exec(
            select(ChunkManifest)
            .where(ChunkManifest.tenant_id == self.tenant_ctx.tenant_id)
            .where(ChunkManifest.id == index_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if manifest is None or manifest.document_id != document_id:
            raise NotFoundError("Index does not belong to this document")
        doc = self.require_in_kb(document_id, manifest.kb_id)
        require_manifest_contract(self.session, self.tenant_ctx, manifest)
        require_reconciled_manifest(manifest, expected_projection_digest=expected_projection_digest)
        statement = (
            update(DocumentRecord)
            .where(DocumentRecord.tenant_id == self.tenant_ctx.tenant_id)
            .where(DocumentRecord.kb_id == manifest.kb_id)
            .where(DocumentRecord.id == document_id)
            .where(DocumentRecord.active_index_id.is_not_distinct_from(expected_active_index_id))
            .values(active_index_id=index_id, ingest_status="indexed", updated_at=utc_now())
            .returning(DocumentRecord.id)
            .execution_options(synchronize_session=False)
        )
        if self.session.execute(statement).scalar_one_or_none() is None:
            raise ConflictError(
                "Active index changed; publication must be retried against its current value"
            )
        self.session.refresh(doc)
        return doc


class DocumentVersionRepository(TenantRepository[DocumentVersion]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session, tenant_ctx, DocumentVersion)

    def scoped_statement(self):
        statement = super().scoped_statement()
        if self.tenant_ctx.allowed_kb_ids is not None:
            statement = statement.where(DocumentVersion.kb_id.in_(self.tenant_ctx.allowed_kb_ids))
        return statement

    def add(self, record: DocumentVersion) -> DocumentVersion:
        raise ConflictError(
            "Immutable source records must be registered through their validated contract"
        )

    def register(
        self,
        *,
        kb_id: UUID,
        document_id: UUID,
        source_digest: str,
        source_ref: str,
        media_type: str,
    ) -> DocumentVersion:
        DocumentRepository(self.session, self.tenant_ctx).require_in_kb(document_id, kb_id)
        require_digest(source_digest)
        require_artifact_ref(source_ref, self.tenant_ctx.tenant_id)
        if (
            not isinstance(media_type, str)
            or re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type) is None
            or len(media_type) > 128
        ):
            raise ValidationError("source media type must be a canonical MIME type")
        fields = dict(
            kb_id=kb_id,
            document_id=document_id,
            source_digest=source_digest,
            source_ref=source_ref,
            media_type=media_type,
        )
        statement = (
            self.scoped_statement()
            .where(DocumentVersion.document_id == document_id)
            .where(DocumentVersion.source_digest == source_digest)
        )
        return _register_immutable(
            self.session, self.tenant_ctx, DocumentVersion, statement, fields
        )

    def require_in_document(
        self, *, version_id: UUID, kb_id: UUID, document_id: UUID
    ) -> DocumentVersion:
        DocumentRepository(self.session, self.tenant_ctx).require_in_kb(document_id, kb_id)
        version = self.require_by_id(version_id)
        if version.kb_id != kb_id or version.document_id != document_id:
            raise NotFoundError("Document version does not belong to this document")
        return version


class DocumentRepresentationRepository(TenantRepository[DocumentRepresentation]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session, tenant_ctx, DocumentRepresentation)

    def scoped_statement(self):
        statement = super().scoped_statement()
        if self.tenant_ctx.allowed_kb_ids is not None:
            statement = statement.where(
                DocumentRepresentation.kb_id.in_(self.tenant_ctx.allowed_kb_ids)
            )
        return statement

    def add(self, record: DocumentRepresentation) -> DocumentRepresentation:
        raise ConflictError(
            "Immutable source records must be registered through their validated contract"
        )

    def register(
        self,
        *,
        kb_id: UUID,
        document_id: UUID,
        document_version_id: UUID,
        representation_digest: str,
        representation_ref: str,
        parser_version: str,
        render_version: str,
        parent_unit: str,
        char_count: int,
        page_count: int | None = None,
    ) -> DocumentRepresentation:
        version = DocumentVersionRepository(self.session, self.tenant_ctx).require_in_document(
            version_id=document_version_id, kb_id=kb_id, document_id=document_id
        )
        require_digest(representation_digest)
        require_artifact_ref(representation_ref, self.tenant_ctx.tenant_id)
        require_profile(parser_version, name="parser version")
        require_profile(render_version, name="render version")
        require_pagination(parent_unit=parent_unit, char_count=char_count, page_count=page_count)
        if (
            parent_unit == "page"
            and version.media_type
            not in {
                "application/pdf",
                "application/vnd.ms-powerpoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            and render_version == "none"
        ):
            raise ValidationError("Pages require a stable source or frozen rendered representation")
        fields = dict(
            kb_id=kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
            representation_digest=representation_digest,
            representation_ref=representation_ref,
            parser_version=parser_version,
            render_version=render_version,
            parent_unit=parent_unit,
            char_count=char_count,
            page_count=page_count,
        )
        statement = (
            self.scoped_statement()
            .where(DocumentRepresentation.document_version_id == document_version_id)
            .where(DocumentRepresentation.representation_digest == representation_digest)
            .where(DocumentRepresentation.parser_version == parser_version)
            .where(DocumentRepresentation.render_version == render_version)
        )
        return _register_immutable(
            self.session, self.tenant_ctx, DocumentRepresentation, statement, fields
        )

    def require_in_version(
        self, *, representation_id: UUID, version_id: UUID, kb_id: UUID, document_id: UUID
    ) -> DocumentRepresentation:
        DocumentVersionRepository(self.session, self.tenant_ctx).require_in_document(
            version_id=version_id, kb_id=kb_id, document_id=document_id
        )
        representation = self.require_by_id(representation_id)
        if (
            representation.kb_id,
            representation.document_id,
            representation.document_version_id,
        ) != (kb_id, document_id, version_id):
            raise NotFoundError("Representation does not belong to this document version")
        return representation


def _register_immutable(session, tenant_ctx, model, statement, fields):
    def existing():
        row = session.exec(statement).first()
        if row is not None and any(getattr(row, key) != value for key, value in fields.items()):
            raise ConflictError("Immutable source identity was reused with different attributes")
        return row

    row = existing()
    if row is not None:
        return row
    row = model(tenant_id=tenant_ctx.tenant_id, **fields)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        row = existing()
        if row is None:
            raise
    session.refresh(row)
    return row


def require_manifest_contract(
    session: Session, tenant_ctx: TenantContext, manifest: ChunkManifest
) -> DocumentRepresentation:
    if manifest.tenant_id != tenant_ctx.tenant_id:
        raise PermissionDeniedError("Index tenant does not match")
    if manifest.document_version_id is None or manifest.representation_id is None:
        raise ValidationError("Unversioned index is unavailable")
    representation = DocumentRepresentationRepository(session, tenant_ctx).require_in_version(
        representation_id=manifest.representation_id,
        version_id=manifest.document_version_id,
        kb_id=manifest.kb_id,
        document_id=manifest.document_id,
    )
    require_digest(manifest.chunker_config_digest)
    require_digest(manifest.plan_digest)
    require_artifact_ref(manifest.plan_ref, tenant_ctx.tenant_id)
    require_profile(manifest.chunker_version, name="chunker version", max_length=64)
    require_profile(manifest.embedding_profile, name="embedding profile", max_length=64)
    require_profile(manifest.embedding_version, name="embedding version")
    if (manifest.parent_unit, manifest.page_count, manifest.parser_version) != (
        representation.parent_unit,
        representation.page_count,
        representation.parser_version,
    ):
        raise ValidationError("Index configuration does not match its representation")
    if (
        isinstance(manifest.chunk_total, bool)
        or not isinstance(manifest.chunk_total, int)
        or manifest.chunk_total < 1
    ):
        raise ValidationError("Planned chunk total must be positive")
    return representation


def require_reconciled_manifest(
    manifest: ChunkManifest, *, expected_projection_digest: str
) -> None:
    """Require durable readback facts; the runtime proves the facts against its plan."""
    require_digest(expected_projection_digest)
    require_digest(manifest.pg_digest)
    require_digest(manifest.vector_digest)
    if (
        manifest.indexing_state != "indexed"
        or manifest.pg_digest != expected_projection_digest
        or manifest.vector_digest != expected_projection_digest
        or manifest.reconciled_at is None
        or manifest.reconciled_at.tzinfo is None
        or manifest.reconciled_at.utcoffset() is None
    ):
        raise ConflictError("Only an index reconciled against its frozen plan can be published")


class ChunkManifestRepository(TenantRepository[ChunkManifest]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=ChunkManifest)

    def scoped_statement(self):
        statement = super().scoped_statement()
        if self.tenant_ctx.allowed_kb_ids is not None:
            statement = statement.where(ChunkManifest.kb_id.in_(self.tenant_ctx.allowed_kb_ids))
        return statement

    def list_for_document(self, document_id: UUID) -> list[ChunkManifest]:
        document = DocumentRepository(self.session, self.tenant_ctx).require_by_id(document_id)
        require_kb(self.session, self.tenant_ctx, document.kb_id)
        stmt = self.scoped_statement().where(ChunkManifest.document_id == document_id)
        return list(self.session.exec(stmt).all())

    def create(
        self,
        *,
        index_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        document_version_id: UUID,
        representation_id: UUID,
        chunker_version: str,
        chunker_config_digest: str,
        embedding_profile: str,
        embedding_version: str,
        plan_ref: str,
        plan_digest: str,
        chunk_total: int,
    ) -> ChunkManifest:
        if not isinstance(index_id, UUID):
            raise ValidationError("A server-generated frozen index UUID is required")
        representation = DocumentRepresentationRepository(
            self.session, self.tenant_ctx
        ).require_in_version(
            representation_id=representation_id,
            version_id=document_version_id,
            kb_id=kb_id,
            document_id=document_id,
        )
        fields = dict(
            id=index_id,
            kb_id=kb_id,
            document_id=document_id,
            document_version_id=document_version_id,
            representation_id=representation_id,
            chunker_version=chunker_version,
            chunker_config_digest=chunker_config_digest,
            embedding_profile=embedding_profile,
            embedding_version=embedding_version,
            plan_ref=plan_ref,
            plan_digest=plan_digest,
            chunk_total=chunk_total,
            parent_unit=representation.parent_unit,
            page_count=representation.page_count,
            parser_version=representation.parser_version,
        )
        require_manifest_contract(
            self.session,
            self.tenant_ctx,
            ChunkManifest(tenant_id=self.tenant_ctx.tenant_id, **fields),
        )
        statement = self.scoped_statement().where(ChunkManifest.id == index_id)
        try:
            return _register_immutable(
                self.session, self.tenant_ctx, ChunkManifest, statement, fields
            )
        except IntegrityError as exc:
            raise ConflictError("Frozen index identifier is unavailable") from exc

    def add(self, record: ChunkManifest) -> ChunkManifest:
        raise ConflictError("New index manifests require an immutable prepared plan")

    def mark_failed(self, manifest_id: UUID, *, error_message: str) -> ChunkManifest:
        manifest = self.session.exec(
            self.scoped_statement()
            .where(ChunkManifest.id == manifest_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if manifest is None:
            raise NotFoundError("Index not found")
        require_manifest_contract(self.session, self.tenant_ctx, manifest)
        if manifest.indexing_state not in {"pending", "indexing", "failed"}:
            raise ConflictError("A published or unversioned index cannot be marked failed")
        manifest.indexing_state = "failed"
        manifest.error_message = error_message
        manifest.updated_at = utc_now()
        self.session.add(manifest)
        self.session.flush()
        self.session.refresh(manifest)
        return manifest
