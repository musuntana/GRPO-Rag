from __future__ import annotations

from uuid import UUID

from ragos.core.errors import ConflictError, NotFoundError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import ChunkManifest, DocumentRecord, KnowledgeBase
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from sqlmodel import Session


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

    def list_for_kb(
        self,
        kb_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        stmt = (
            self.scoped_statement()
            .where(DocumentRecord.kb_id == kb_id)
            .offset(offset)
            .limit(limit)
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
        doc = self.require_by_id(document_id)
        if doc.kb_id != kb_id:
            raise NotFoundError(
                "Document does not belong to the given knowledge base",
                resource="DocumentRecord",
                id=str(document_id),
                kb_id=str(kb_id),
            )
        return doc


class ChunkManifestRepository(TenantRepository[ChunkManifest]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=ChunkManifest)

    def list_for_document(self, document_id: UUID) -> list[ChunkManifest]:
        stmt = self.scoped_statement().where(ChunkManifest.document_id == document_id)
        return list(self.session.exec(stmt).all())

    def create(
        self,
        *,
        kb_id: UUID,
        document_id: UUID,
    ) -> ChunkManifest:
        manifest = ChunkManifest(
            tenant_id=self.tenant_ctx.tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_total=0,
            indexing_state="pending",
        )
        self.session.add(manifest)
        self.session.flush()
        self.session.refresh(manifest)
        return manifest

    def mark_failed(self, manifest_id: UUID, *, error_message: str) -> ChunkManifest:
        manifest = self.require_by_id(manifest_id)
        manifest.indexing_state = "failed"
        manifest.error_message = error_message
        manifest.updated_at = utc_now()
        self.session.add(manifest)
        self.session.flush()
        self.session.refresh(manifest)
        return manifest
