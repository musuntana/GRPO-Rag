from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.config import Settings
from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime import ingest_job, worker_embedding
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos_ingest_worker.main import build_registry

MODEL = "ragos/qwen3-embedding:0.6b-q8"
CHAT_MODEL = "ai/qwen3:4b-instruct"
DIGEST = "c" * 40
PROFILE = "qwen3_embedding"
DIMENSION = 3
BASE_URL = "http://127.0.0.1:12434/v1"


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


def _dev_settings(**overrides) -> Settings:
    spec = FrozenEmbeddingSpec(
        model=MODEL,
        model_digest=DIGEST,
        dimension=DIMENSION,
        profile=PROFILE,
    )
    values = {
        "_env_file": None,
        "app_env": "dev",
        "api_host": "127.0.0.1",
        "ingest_local_model_enabled": True,
        "local_model_enabled": True,
        "local_model_provider_id": uuid4(),
        "local_model_base_url": BASE_URL,
        "local_model_chat": CHAT_MODEL,
        "local_model_embed": MODEL,
        "local_model_embed_revision": DIGEST,
        "local_model_embed_dimension": DIMENSION,
        "embedding_profile": PROFILE,
        "embedding_version": spec.version,
        "qdrant_url": "http://127.0.0.1:6333",
    }
    values.update(overrides)
    return Settings(**values)


class _SessionCtx:
    def __init__(self, session) -> None:
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc_info) -> None:
        return None


class _Session:
    def begin(self):
        return nullcontext()


class _Factory:
    def __init__(self) -> None:
        self.sessions = []

    def __call__(self):
        session = _Session()
        self.sessions.append(session)
        return _SessionCtx(session)


class _TaskRepo:
    def __init__(self, task) -> None:
        self.task = task
        self.calls = []

    def lock_claim(self, task_id, *, expected_attempts, expected_tenant_id):
        self.calls.append((task_id, expected_attempts, expected_tenant_id))
        if (
            task_id != self.task.id
            or expected_attempts != self.task.attempts
            or expected_tenant_id != self.task.tenant_id
        ):
            raise ConflictError("Task claim fence changed")
        return self.task


def _claim(monkeypatch, task):
    factory = _Factory()
    repo = _TaskRepo(task)
    context = TenantContext(
        tenant_id=task.tenant_id,
        tenant_slug="acme",
        actor_id="ingest-worker",
        trace_id="worker-test",
    )
    monkeypatch.setattr(ingest_job, "PlatformTaskRepository", lambda session: repo)
    monkeypatch.setattr(ingest_job, "bind_session_tenant", lambda session, tenant_id: None)
    monkeypatch.setattr(
        ingest_job, "build_worker_tenant_ctx", lambda *args, **kwargs: context
    )
    return factory, repo, context


def _task(*, kind="ingest_document", payload=None, tenant_id=None, attempts=1):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id or uuid4(),
        kind=kind,
        payload={"manifest_id": str(uuid4())} if payload is None else payload,
        attempts=attempts,
    )


def test_claim_keeps_the_task_manifest_kind_and_payload_guards(monkeypatch):
    manifest_id = uuid4()
    task = _task(payload={"manifest_id": str(manifest_id)})
    factory, repo, context = _claim(monkeypatch, task)
    claimed_manifest, claimed_context = ingest_job._claim_job(
        factory=factory, tenant_id=task.tenant_id, task_id=task.id, attempt=task.attempts
    )
    assert claimed_manifest == manifest_id
    assert claimed_context is context
    assert repo.calls == [(task.id, task.attempts, task.tenant_id)]


@pytest.mark.parametrize(
    "task",
    [
        _task(kind="prepare_document"),
        _task(payload={"manifest_id": str(uuid4()), "url": "http://example.test"}),
        _task(payload={"manifest_id": str(uuid4()), "tenant_id": str(uuid4())}),
    ],
)
def test_claim_refuses_untrusted_task_identity_from_payload_or_kind(monkeypatch, task):
    factory, _repo, _context = _claim(monkeypatch, task)
    with pytest.raises(ValidationError, match="authorized immutable index job"):
        ingest_job._claim_job(
            factory=factory, tenant_id=task.tenant_id, task_id=task.id, attempt=task.attempts
        )


def test_claim_refuses_a_task_from_another_tenant_or_attempt(monkeypatch):
    task = _task()
    factory, _repo, _context = _claim(monkeypatch, task)
    with pytest.raises(ConflictError, match="fence"):
        ingest_job._claim_job(
            factory=factory, tenant_id=uuid4(), task_id=task.id, attempt=task.attempts
        )
    with pytest.raises(ConflictError, match="fence"):
        ingest_job._claim_job(
            factory=factory, tenant_id=task.tenant_id, task_id=task.id, attempt=task.attempts + 1
        )


def test_manifest_precheck_requires_the_trusted_worker_identity(monkeypatch):
    manifest = SimpleNamespace(embedding_profile=PROFILE, embedding_version="other")
    monkeypatch.setattr(
        ingest_job,
        "ChunkManifestRepository",
        lambda session, context: SimpleNamespace(require_by_id=lambda manifest_id: manifest),
    )
    monkeypatch.setattr(ingest_job, "bind_session_tenant", lambda session, tenant_id: None)
    context = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="ingest-worker", trace_id="t"
    )
    with pytest.raises(ValidationError, match="frozen index manifest"):
        ingest_job._require_manifest_embedding(
            factory=_Factory(),
            context=context,
            manifest_id=uuid4(),
            profile=PROFILE,
            version="expected",
        )


def test_hash_path_keeps_the_existing_test_writer_and_closes_it(monkeypatch):
    settings = Settings(
        app_env="test",
        task_test_embedding=True,
        qdrant_url="http://127.0.0.1:6333",
    )
    factory = _Factory()
    manifest_id = uuid4()
    context = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="ingest-worker", trace_id="t"
    )
    events = []
    captured = {}

    monkeypatch.setattr(ingest_job, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(ingest_job, "get_session_factory", lambda configured: factory)
    monkeypatch.setattr(
        ingest_job, "_claim_job", lambda **kwargs: (manifest_id, context)
    )
    monkeypatch.setattr(
        ingest_job,
        "_require_manifest_embedding",
        lambda **kwargs: pytest.fail("hash path must not resolve the real gateway"),
    )
    monkeypatch.setattr(
        ingest_job,
        "WorkerEmbedding",
        lambda *args, **kwargs: pytest.fail("hash path must not build a worker adapter"),
    )

    class Writer:
        def __init__(self, **kwargs) -> None:
            captured["writer"] = self
            self.kwargs = kwargs
            self.client = SimpleNamespace(close=lambda: events.append("close"))

    class Service:
        def __init__(self, factory_arg, context_arg, **kwargs) -> None:
            assert factory_arg is factory
            assert context_arg is context
            self.writer = kwargs["vector_writer"]
            assert callable(kwargs["execution_guard"])

        def execute(self, *, manifest_id):
            events.append(("execute", manifest_id))

    monkeypatch.setattr(ingest_job, "QdrantVectorIndexWriter", Writer)
    monkeypatch.setattr(ingest_job, "IngestionService", Service)

    ingest_job.execute_job(tenant_id=context.tenant_id, task_id=uuid4(), attempt=1)

    assert captured["writer"].kwargs == {
        "url": settings.qdrant_url,
        "embedding_profile": "hash-test",
        "embedding_version": "sha256-hash-v1",
        "allow_test_embedding": True,
    }
    assert events == [("execute", manifest_id), "close"]


def test_dev_path_uses_the_pinned_worker_writer_and_closes_it(monkeypatch):
    settings = _dev_settings()
    monkeypatch.setattr(worker_embedding.sys, "platform", "linux")
    factory = _Factory()
    manifest_id = uuid4()
    context = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="ingest-worker", trace_id="t"
    )
    events = []
    captured = {}

    monkeypatch.setattr(ingest_job, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(ingest_job, "get_session_factory", lambda configured: factory)
    monkeypatch.setattr(
        ingest_job, "_claim_job", lambda **kwargs: (manifest_id, context)
    )

    def require_manifest(**kwargs):
        events.append("manifest")
        assert kwargs["factory"] is factory
        assert kwargs["context"] is context
        assert kwargs["manifest_id"] == manifest_id
        assert kwargs["profile"] == PROFILE
        assert kwargs["version"] == settings.embedding_version

    monkeypatch.setattr(ingest_job, "_require_manifest_embedding", require_manifest)

    class Worker:
        def __init__(self, settings_arg, factory_arg, context_arg) -> None:
            assert settings_arg is settings
            assert factory_arg is factory
            assert context_arg is context
            self.profile = PROFILE
            self.version = settings.embedding_version
            self.dimension = DIMENSION
            captured["worker"] = self
            events.append("worker")

        def preflight(self):
            events.append("preflight")

        def embed_one(self, text):
            return [1.0, 0.0, 0.0]

    class Writer:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.client = SimpleNamespace(close=lambda: events.append("close"))
            captured["writer"] = self
            events.append("writer")

    class Service:
        def __init__(self, factory_arg, context_arg, **kwargs) -> None:
            assert factory_arg is factory
            assert context_arg is context
            assert kwargs["vector_writer"] is captured["writer"]
            assert callable(kwargs["execution_guard"])

        def execute(self, *, manifest_id):
            events.append(("execute", manifest_id))

    monkeypatch.setattr(ingest_job, "WorkerEmbedding", Worker)
    monkeypatch.setattr(ingest_job, "QdrantVectorIndexWriter", Writer)
    monkeypatch.setattr(ingest_job, "IngestionService", Service)

    ingest_job.execute_job(tenant_id=context.tenant_id, task_id=uuid4(), attempt=1)

    worker = captured["worker"]
    assert events == [
        "worker",
        "preflight",
        "manifest",
        "writer",
        ("execute", manifest_id),
        "close",
    ]
    assert captured["writer"].kwargs == {
        "url": settings.qdrant_url,
        "embedding_dim": DIMENSION,
        "embedding_fn": worker.embed_one,
        "embedding_profile": PROFILE,
        "embedding_version": settings.embedding_version,
    }
    assert captured["writer"].kwargs.get("allow_test_embedding") is None


def test_vector_client_is_closed_after_an_ingestion_failure(monkeypatch):
    settings = Settings(
        app_env="test",
        task_test_embedding=True,
        qdrant_url="http://127.0.0.1:6333",
    )
    factory = _Factory()
    context = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="ingest-worker", trace_id="t"
    )
    events = []

    monkeypatch.setattr(ingest_job, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(ingest_job, "get_session_factory", lambda configured: factory)
    monkeypatch.setattr(
        ingest_job, "_claim_job", lambda **kwargs: (uuid4(), context)
    )

    class Writer:
        def __init__(self, **kwargs) -> None:
            self.client = SimpleNamespace(close=lambda: events.append("close"))

    class Service:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def execute(self, *, manifest_id):
            raise RuntimeError("index write failed")

    monkeypatch.setattr(ingest_job, "QdrantVectorIndexWriter", Writer)
    monkeypatch.setattr(ingest_job, "IngestionService", Service)

    with pytest.raises(RuntimeError, match="index write failed"):
        ingest_job.execute_job(tenant_id=context.tenant_id, task_id=uuid4(), attempt=1)
    assert events == ["close"]


def test_dev_worker_child_env_is_an_explicit_server_allowlist(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-inherit")
    monkeypatch.setenv("RAGOS_LOCAL_MODEL_BASE_URL", "http://attacker.test/v1")
    monkeypatch.setattr(worker_embedding.sys, "platform", "linux")
    settings = _dev_settings()

    registry = build_registry(settings)
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="worker", trace_id="t")
    task = SimpleNamespace(
        id=uuid4(), tenant_id=ctx.tenant_id, kind="ingest_document", attempts=1
    )
    command = registry.resolve("ingest_document")(task=task, tenant_ctx=ctx)

    assert set(command.env) == {
        "RAGOS_APP_ENV",
        "RAGOS_INGEST_LOCAL_MODEL_ENABLED",
        "RAGOS_LOCAL_MODEL_ENABLED",
        "RAGOS_LOCAL_MODEL_PROVIDER_ID",
        "RAGOS_LOCAL_MODEL_BASE_URL",
        "RAGOS_LOCAL_MODEL_CHAT",
        "RAGOS_LOCAL_MODEL_EMBED",
        "RAGOS_LOCAL_MODEL_EMBED_REVISION",
        "RAGOS_LOCAL_MODEL_EMBED_DIMENSION",
        "RAGOS_API_HOST",
        "RAGOS_PG_DSN",
        "RAGOS_QDRANT_URL",
        "RAGOS_STORAGE_ROOT",
        "RAGOS_EMBEDDING_PROFILE",
        "RAGOS_EMBEDDING_VERSION",
    }
    assert command.env["RAGOS_APP_ENV"] == "dev"
    assert command.env["RAGOS_INGEST_LOCAL_MODEL_ENABLED"] == "true"
    assert command.env["RAGOS_LOCAL_MODEL_BASE_URL"] == settings.local_model_base_url
    assert command.env["RAGOS_LOCAL_MODEL_EMBED_REVISION"] == DIGEST
    assert command.env["RAGOS_LOCAL_MODEL_EMBED_DIMENSION"] == str(DIMENSION)
    assert command.env["RAGOS_EMBEDDING_PROFILE"] == PROFILE
    assert command.env["RAGOS_EMBEDDING_VERSION"] == settings.embedding_version
    assert "UNRELATED_SECRET" not in command.env
    assert command.replay_safe is True


def test_dev_worker_refuses_default_disabled_and_production():
    with pytest.raises(RuntimeError, match="ingest_local_model_enabled"):
        build_registry(_dev_settings(ingest_local_model_enabled=False))
    with pytest.raises(RuntimeError, match="real gateway"):
        build_registry(_dev_settings(app_env="production"))
