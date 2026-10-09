from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.config import Settings
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime import ingest_job
from ragos_ingest_worker.main import build_registry


def test_ingest_job_refuses_unconnected_production_embedding_before_database(monkeypatch):
    monkeypatch.setattr(ingest_job, "Settings", lambda **kwargs: Settings(app_env="production"))
    monkeypatch.setattr(ingest_job, "get_session_factory", lambda *args: pytest.fail("no database"))
    with pytest.raises(ValidationError, match="real gateway"):
        ingest_job.execute_job(tenant_id=uuid4(), task_id=uuid4(), attempt=1)


def test_ingest_test_hash_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(
        ingest_job, "Settings", lambda **kwargs: Settings(app_env="test", task_test_embedding=False)
    )
    with pytest.raises(ValidationError, match="real gateway"):
        ingest_job.execute_job(tenant_id=uuid4(), task_id=uuid4(), attempt=1)


def test_test_worker_refuses_another_embedding_space_before_claiming():
    with pytest.raises(RuntimeError, match="frozen hash embedding identity"):
        build_registry(
            Settings(
                app_env="test",
                task_test_embedding=True,
                qdrant_url="http://127.0.0.1:6333",
                embedding_version="other",
            )
        )


def test_child_receives_server_embedding_identity_without_ambient_secrets(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-inherit")
    settings = Settings(
        app_env="test", task_test_embedding=True, qdrant_url="http://127.0.0.1:6333"
    )
    registry = build_registry(settings)
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="worker", trace_id="t")
    task = SimpleNamespace(id=uuid4(), tenant_id=ctx.tenant_id, kind="ingest_document", attempts=1)
    command = registry.resolve("ingest_document")(task=task, tenant_ctx=ctx)
    assert command.env["RAGOS_EMBEDDING_PROFILE"] == "hash-test"
    assert command.env["RAGOS_EMBEDDING_VERSION"] == "sha256-hash-v1"
    assert "UNRELATED_SECRET" not in command.env
