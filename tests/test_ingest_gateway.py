"""Worker-side real-gateway embedding boundary; no model or database is contacted."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from ragos.core.config import Settings
from ragos.core.errors import ProviderError, ValidationError
from ragos.core.tenant import ActorType, TenantContext
from ragos.model_gateway.local_admission import LocalModelAdmission, LocalPinnedProvider
from ragos.model_gateway.providers import EmbedResult
from ragos.runtime import worker_embedding
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos.runtime.worker_embedding import (
    LOCAL_GATEWAY_MODE,
    WorkerEmbedding,
    validate_worker_embedding_settings,
)

MODEL = "ragos/qwen3-embedding:0.6b-q8"
CHAT_MODEL = "ai/qwen3:4b-instruct"
DIGEST = "a" * 40
PROFILE = "qwen3_embedding"
DIMENSION = 3
BASE_URL = "http://127.0.0.1:12434/v1"


def _spec(**overrides) -> FrozenEmbeddingSpec:
    values = {
        "model": MODEL,
        "model_digest": DIGEST,
        "dimension": DIMENSION,
        "profile": PROFILE,
    }
    values.update(overrides)
    return FrozenEmbeddingSpec(**values)


def _dev_settings(**overrides) -> Settings:
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
        "embedding_version": _spec().version,
    }
    values.update(overrides)
    return Settings(**values)


def _ctx(tenant_id: UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid4(),
        tenant_slug="acme",
        actor_id="ingest-worker",
        actor_type=ActorType.SERVICE,
        trace_id="worker-test",
    )


def _patch_linux(monkeypatch) -> None:
    monkeypatch.setattr(worker_embedding.sys, "platform", "linux")


class _SessionCtx:
    def __init__(self, session) -> None:
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc_info) -> None:
        return None


class _Session:
    def __init__(self, index: int) -> None:
        self.index = index

    def begin(self):
        return nullcontext()


class _Factory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self):
        session = _Session(len(self.sessions))
        self.sessions.append(session)
        return _SessionCtx(session)


@dataclass
class _Route:
    provider: object
    provider_slug: str
    model_name: str
    parameters: dict
    profile_id: str


class _Router:
    created: list[_Router] = []
    vectors: list[list[list[float]]] = []
    resolves: list[tuple[object, TenantContext, str]] = []
    embeds: list[tuple[object, TenantContext, list[str]]] = []
    route_model = MODEL
    route_parameters: dict = {}
    provider: object | None = None

    def __init__(self, session, *, allow_test_stub=False, local_model=None, settings=None) -> None:
        self.session = session
        self.settings = settings
        self.allow_test_stub = allow_test_stub
        self.local_model = local_model
        type(self).created.append(self)

    def resolve(self, tenant_ctx, use_case, *, profile_override=None):
        type(self).resolves.append((self.session, tenant_ctx, use_case))
        return _Route(
            provider=type(self).provider,
            provider_slug="local-dmr",
            model_name=type(self).route_model,
            parameters=type(self).route_parameters,
            profile_id=str(uuid4()),
        )

    def embed(self, tenant_ctx, *, texts, use_case="embed", profile_override=None):
        type(self).embeds.append((self.session, tenant_ctx, list(texts)))
        return EmbedResult(embeddings=type(self).vectors.pop(0), model=MODEL)


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch):
    monkeypatch.setattr(
        worker_embedding, "bind_session_tenant", lambda session, tenant_id: None
    )
    _Router.created = []
    _Router.vectors = []
    _Router.resolves = []
    _Router.embeds = []
    _Router.route_model = MODEL
    _Router.route_parameters = {}
    _Router.provider = None
    yield


def _pinned_provider(settings: Settings) -> LocalPinnedProvider:
    return LocalPinnedProvider(
        LocalModelAdmission(
            provider_id=settings.local_model_provider_id,
            base_url=settings.local_model_base_url,
            chat_model=settings.local_model_chat,
            embedding_model=settings.local_model_embed,
            embedding_dimension=settings.local_model_embed_dimension,
        )
    )


def _worker(monkeypatch, settings: Settings | None = None):
    _patch_linux(monkeypatch)
    settings = settings or _dev_settings()
    _Router.provider = _pinned_provider(settings)
    monkeypatch.setattr(worker_embedding, "ModelRouter", _Router)
    factory = _Factory()
    context = _ctx()
    return WorkerEmbedding(settings, factory, context), factory, context, settings


def test_selected_mode_refuses_production_before_any_resource() -> None:
    settings = _dev_settings(app_env="production")
    with pytest.raises(ValidationError, match="real gateway"):
        validate_worker_embedding_settings(settings)


def test_selected_mode_is_disabled_by_default() -> None:
    assert Settings(_env_file=None).ingest_local_model_enabled is False
    with pytest.raises(ValidationError, match="ingest_local_model_enabled"):
        validate_worker_embedding_settings(_dev_settings(ingest_local_model_enabled=False))


@pytest.mark.parametrize(
    "overrides",
    [
        {"local_model_enabled": False},
        {"local_model_provider_id": None},
        {"local_model_base_url": ""},
        {"api_host": "0.0.0.0"},
    ],
)
def test_selected_mode_requires_a_valid_local_admission(monkeypatch, overrides) -> None:
    _patch_linux(monkeypatch)
    with pytest.raises(ValidationError):
        validate_worker_embedding_settings(_dev_settings(**overrides))


def test_selected_mode_refuses_non_linux_real_execution(monkeypatch) -> None:
    monkeypatch.setattr(worker_embedding.sys, "platform", "darwin")
    with pytest.raises(ValidationError, match="Linux"):
        validate_worker_embedding_settings(_dev_settings())


def test_selected_mode_requires_the_configured_frozen_identity(monkeypatch) -> None:
    _patch_linux(monkeypatch)
    with pytest.raises(ValidationError, match="frozen"):
        validate_worker_embedding_settings(_dev_settings(embedding_version="b" * 40))


def test_selected_mode_uses_trusted_frozen_profile_and_version(monkeypatch) -> None:
    _patch_linux(monkeypatch)
    settings = _dev_settings()
    assert validate_worker_embedding_settings(settings) == LOCAL_GATEWAY_MODE
    worker = WorkerEmbedding(settings, _Factory(), _ctx())
    assert worker.spec == _spec()
    assert (worker.profile, worker.version, worker.dimension) == (
        PROFILE,
        _spec().version,
        DIMENSION,
    )


def test_worker_revalidates_the_frozen_identity_even_without_mode_selection(monkeypatch) -> None:
    _patch_linux(monkeypatch)
    with pytest.raises(ValidationError, match="frozen"):
        WorkerEmbedding(_dev_settings(embedding_version="b" * 40), _Factory(), _ctx())


def test_preflight_authorizes_the_tenant_embed_binding_without_a_model_request(monkeypatch) -> None:
    worker, factory, context, settings = _worker(monkeypatch)
    worker.preflight()
    assert len(factory.sessions) == 1
    assert len(_Router.resolves) == 1
    assert _Router.resolves[0][2] == "embed"
    assert _Router.embeds == []
    assert _Router.created[0].settings is settings
    assert _Router.created[0].local_model is None


@pytest.mark.parametrize(
    "provider, model, parameters, message",
    [
        (SimpleNamespace(), MODEL, {}, "admitted"),
        ("pinned", "another-embedding-model", {}, "admitted"),
        ("pinned", MODEL, {"dimension": 3}, "parameters"),
    ],
)
def test_preflight_refuses_a_route_that_differs_from_the_trusted_pin(
    monkeypatch, provider, model, parameters, message
) -> None:
    worker, _factory, _context, settings = _worker(monkeypatch)
    pinned = _pinned_provider(settings)
    _Router.provider = pinned if provider == "pinned" else provider
    _Router.route_model = model
    _Router.route_parameters = parameters
    with pytest.raises(ProviderError, match=message):
        worker.preflight()


@pytest.mark.parametrize(
    "embeddings, message",
    [
        ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], "count"),
        ([[1.0, 0.0]], "dimension"),
        ([[True, 0.0, 0.0]], "vector"),
        ([[float("nan"), 0.0, 0.0]], "vector"),
        ([[0.0, 0.0, 0.0]], "vector"),
        ([["1", 0.0, 0.0]], "vector"),
    ],
)
def test_embed_one_rejects_vectors_outside_the_pinned_contract(
    monkeypatch, embeddings, message
) -> None:
    worker, _factory, _context, _settings = _worker(monkeypatch)
    _Router.vectors = [embeddings]
    with pytest.raises(ProviderError, match=message):
        worker.embed_one("synthetic contract page")


def test_embed_one_uses_a_fresh_bound_tenant_session_per_text(monkeypatch) -> None:
    worker, factory, context, settings = _worker(monkeypatch)
    bound: list[tuple[object, UUID]] = []
    monkeypatch.setattr(
        worker_embedding,
        "bind_session_tenant",
        lambda session, tenant_id: bound.append((session, tenant_id)),
    )
    _Router.vectors = [[[1.0, 0.5, 0.0]], [[0.0, 0.25, 1.0]]]

    first = worker.embed_one("first page")
    second = worker.embed_one("second page")

    assert first == [1.0, 0.5, 0.0]
    assert second == [0.0, 0.25, 1.0]
    assert len(factory.sessions) == 2
    assert len({id(session) for session in factory.sessions}) == 2
    assert [tenant_id for _session, tenant_id in bound] == [context.tenant_id, context.tenant_id]
    assert [embed[0] for embed in _Router.embeds] == factory.sessions
    assert [embed[1] for embed in _Router.embeds] == [context, context]
    assert [embed[2] for embed in _Router.embeds] == [["first page"], ["second page"]]
    assert all(router.settings is settings for router in _Router.created)
