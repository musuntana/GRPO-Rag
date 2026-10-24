"""Trusted isolated ingestion process; CLI references do not themselves grant scope."""

from __future__ import annotations

import argparse
from uuid import UUID

from ragos.core.config import Settings
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime.ingest import IngestionService
from ragos.runtime.tasks import build_worker_tenant_ctx
from ragos.runtime.worker_embedding import (
    HASH_TEST_MODE,
    LOCAL_GATEWAY_MODE,
    WorkerEmbedding,
    validate_worker_embedding_settings,
)
from ragos.storage.artifacts import ArtifactStore
from ragos.storage.db import bind_session_tenant, get_session_factory
from ragos.storage.models.app import Task
from ragos.storage.repositories.kb import ChunkManifestRepository
from ragos.storage.repositories.tasks import PlatformTaskRepository
from ragos.storage.vector import QdrantVectorIndexWriter


def _claim_job(
    *, factory, tenant_id: UUID, task_id: UUID, attempt: int
) -> tuple[UUID, TenantContext]:
    with factory() as session:
        bind_session_tenant(session, tenant_id)
        with session.begin():
            task = PlatformTaskRepository(session).lock_claim(
                task_id, expected_attempts=attempt, expected_tenant_id=tenant_id
            )
            if task.kind != "ingest_document" or set(task.payload) != {"manifest_id"}:
                raise ValidationError("task is not an authorized immutable index job")
            manifest_id = UUID(str(task.payload["manifest_id"]))
            context = build_worker_tenant_ctx(
                tenant_id, session, actor_id="ingest-worker", trace_id=f"worker-{task_id}"
            )
    return manifest_id, context


def _execution_guard(*, tenant_id: UUID, task_id: UUID, attempt: int):
    def guard(session):
        PlatformTaskRepository(session).lock_claim(
            task_id, expected_attempts=attempt, expected_tenant_id=tenant_id
        )
        if not session.get(Task, task_id).kind == "ingest_document":
            raise ValidationError("task kind changed")

    return guard


def _require_manifest_embedding(
    *,
    factory,
    context: TenantContext,
    manifest_id: UUID,
    profile: str,
    version: str,
) -> None:
    """Read the frozen control-plane identity without holding a write lock."""
    with factory() as session:
        bind_session_tenant(session, context.tenant_id)
        with session.begin():
            manifest = ChunkManifestRepository(session, context).require_by_id(manifest_id)
            if (manifest.embedding_profile, manifest.embedding_version) != (
                profile,
                version,
            ):
                raise ValidationError(
                    "frozen index manifest does not match the worker embedding identity"
                )


def execute_job(*, tenant_id: UUID, task_id: UUID, attempt: int) -> None:
    settings = Settings(_env_file=None)
    mode = validate_worker_embedding_settings(settings)
    if not settings.qdrant_url:
        raise ValidationError("ingest process requires an explicitly configured vector service")
    factory = get_session_factory(settings)
    manifest_id, context = _claim_job(
        factory=factory, tenant_id=tenant_id, task_id=task_id, attempt=attempt
    )
    guard = _execution_guard(tenant_id=tenant_id, task_id=task_id, attempt=attempt)
    writer = None
    try:
        if mode == HASH_TEST_MODE:
            writer = QdrantVectorIndexWriter(
                url=settings.qdrant_url,
                embedding_profile=settings.embedding_profile,
                embedding_version=settings.embedding_version,
                allow_test_embedding=True,
            )
        elif mode == LOCAL_GATEWAY_MODE:
            worker = WorkerEmbedding(settings, factory, context)
            # Authorize the tenant embed binding before any vector side effect.
            worker.preflight()
            _require_manifest_embedding(
                factory=factory,
                context=context,
                manifest_id=manifest_id,
                profile=worker.profile,
                version=worker.version,
            )
            writer = QdrantVectorIndexWriter(
                url=settings.qdrant_url,
                embedding_dim=worker.dimension,
                embedding_fn=worker.embed_one,
                embedding_profile=worker.profile,
                embedding_version=worker.version,
            )
        else:
            raise ValidationError("ingest embedding mode is unavailable")

        IngestionService(
            factory,
            context,
            artifacts=ArtifactStore(settings.storage_root),
            vector_writer=writer,
            execution_guard=guard,
        ).execute(manifest_id=manifest_id)
    finally:
        if writer is not None:
            writer.client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args()
    if args.attempt < 1:
        raise ValidationError("task attempt must be positive")
    execute_job(tenant_id=args.tenant_id, task_id=args.task_id, attempt=args.attempt)


if __name__ == "__main__":
    main()
