"""Trusted isolated ingestion process; CLI references do not themselves grant scope."""

from __future__ import annotations

import argparse
from uuid import UUID

from ragos.core.config import Settings
from ragos.core.errors import ValidationError
from ragos.runtime.ingest import IngestionService
from ragos.runtime.tasks import build_worker_tenant_ctx
from ragos.storage.artifacts import ArtifactStore
from ragos.storage.db import bind_session_tenant, get_session_factory
from ragos.storage.models.app import Task
from ragos.storage.repositories.tasks import PlatformTaskRepository
from ragos.storage.vector import QdrantVectorIndexWriter


def execute_job(*, tenant_id: UUID, task_id: UUID, attempt: int) -> None:
    settings = Settings(_env_file=None)
    if settings.app_env != "test" or not settings.task_test_embedding:
        raise ValidationError("ingest process unavailable: real gateway embedding is not connected")
    if (settings.embedding_profile, settings.embedding_version) != ("hash-test", "sha256-hash-v1"):
        raise ValidationError("test ingestion requires the explicit frozen hash embedding identity")
    if not settings.qdrant_url:
        raise ValidationError("ingest process requires an explicitly configured vector service")
    factory = get_session_factory(settings)
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

    def guard(session):
        PlatformTaskRepository(session).lock_claim(
            task_id, expected_attempts=attempt, expected_tenant_id=tenant_id
        )
        if not session.get(Task, task_id).kind == "ingest_document":
            raise ValidationError("task kind changed")

    writer = QdrantVectorIndexWriter(
        url=settings.qdrant_url,
        embedding_profile=settings.embedding_profile,
        embedding_version=settings.embedding_version,
        allow_test_embedding=True,
    )
    IngestionService(
        factory,
        context,
        artifacts=ArtifactStore(settings.storage_root),
        vector_writer=writer,
        execution_guard=guard,
    ).execute(manifest_id=manifest_id)


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
