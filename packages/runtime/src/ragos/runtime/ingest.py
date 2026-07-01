from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ragos.core.tenant import TenantContext
from ragos.storage.models.app import ChunkManifest
from ragos.storage.repositories.base import TenantRepository
from ragos.storage.repositories.search import SearchChunkUpsert, SearchRepository, SearchTier
from sqlmodel import Session


class VectorIndexWriter(Protocol):
    def upsert(
        self,
        *,
        tenant_ctx: TenantContext,
        tier: SearchTier,
        chunks: list[SearchChunkUpsert],
    ) -> None: ...


class NoopVectorIndexWriter:
    def upsert(
        self,
        *,
        tenant_ctx: TenantContext,
        tier: SearchTier,
        chunks: list[SearchChunkUpsert],
    ) -> None:
        return None


@dataclass(slots=True)
class IndexBatchResult:
    manifest_id: UUID
    chunk_total: int
    indexing_state: str


class IngestionService:
    """Dual-writes chunks to the FTS store and the vector index.

    Transaction policy: this service assumes the caller already opened a
    transaction on ``session`` (HTTP handler, worker dispatcher, or job
    entrypoint). It does not call ``session.begin()`` or ``session.commit()``.
    On exception, the outer transaction is expected to roll back — the
    service does not attempt to persist a partial failure marker inside the
    same transaction, because that write would be rolled back too.
    """

    def __init__(
        self,
        session: Session,
        tenant_ctx: TenantContext,
        *,
        vector_writer: VectorIndexWriter | None = None,
    ) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx
        self.manifests = TenantRepository(
            session=session,
            tenant_ctx=tenant_ctx,
            model_type=ChunkManifest,
        )
        self.search_repo = SearchRepository(session=session, tenant_ctx=tenant_ctx)
        self.vector_writer = vector_writer or NoopVectorIndexWriter()

    def index_chunks(
        self,
        *,
        manifest_id: UUID,
        tier: SearchTier,
        chunks: list[SearchChunkUpsert],
    ) -> IndexBatchResult:
        manifest = self.manifests.require_by_id(manifest_id)
        manifest.indexing_state = "indexing"
        manifest.error_message = None
        self.session.add(manifest)
        self.session.flush()

        self.search_repo.upsert_chunks(tier=tier, chunks=chunks)
        self.vector_writer.upsert(
            tenant_ctx=self.tenant_ctx,
            tier=tier,
            chunks=chunks,
        )

        manifest.chunk_total = len(chunks)
        manifest.indexing_state = "indexed"
        self.session.add(manifest)
        self.session.flush()
        return IndexBatchResult(
            manifest_id=manifest.id,
            chunk_total=manifest.chunk_total,
            indexing_state=manifest.indexing_state,
        )
