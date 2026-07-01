from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from ragos.core.errors import ProviderError
from ragos.core.tenant import ActorType, TenantContext
from ragos.model_gateway.key_pool import KeyLease, KeyPoolManager
from ragos.model_gateway.providers import ResolvedRoute
from ragos.model_gateway.providers.stub import StubProvider
from ragos.model_gateway.router import ModelRouter
from ragos.storage.models.app import KeyPool, Provider

TENANT_ID = UUID("fa9d4df0-81ce-4f1a-9820-4c29259779a6")


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        actor_id="u1",
        actor_type=ActorType.TENANT_ADMIN,
        roles=["tenant_admin"],
        trace_id="t-1",
    )


# ---------------------------------------------------------------------------
# StubProvider
# ---------------------------------------------------------------------------


def test_stub_provider_chat_returns_empty() -> None:
    p = StubProvider(api_key="sk-test")
    result = p.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4")
    assert result.content == ""
    assert result.model == "gpt-4"


def test_stub_provider_embed_returns_zeros() -> None:
    p = StubProvider(api_key="sk-test")
    result = p.embed(texts=["a", "b"], model="text-embedding-3")
    assert len(result.embeddings) == 2
    assert result.model == "text-embedding-3"


def test_stub_provider_rerank_returns_all() -> None:
    p = StubProvider(api_key="sk-test")
    result = p.rerank(query="q", documents=["a", "b", "c"], model="rerank-1")
    assert len(result) == 3


def test_stub_provider_health() -> None:
    assert StubProvider(api_key="sk-test").health() is True


# ---------------------------------------------------------------------------
# KeyPoolManager — weighted selection via monkeypatching
# ---------------------------------------------------------------------------


def _make_key(pool_id: UUID, weight: int, label: str = "k") -> KeyPool:
    return KeyPool(
        id=uuid4(),
        provider_id=pool_id,
        label=label,
        api_key_encrypted="sk-" + label,
        weight=weight,
    )


def _make_provider(pid: UUID, slug: str = "oai", provider_type: str = "openai") -> Provider:
    return Provider(
        id=pid,
        slug=slug,
        provider_type=provider_type,
        display_name=slug.upper(),
    )


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        pass


def test_key_pool_weights_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid4()
    keys = [_make_key(pid, weight=2, label="k1"), _make_key(pid, weight=1, label="k2")]
    provider = _make_provider(pid)

    session = _FakeSession()
    mgr = KeyPoolManager(session)

    # Patch _list_active_keys to return our keys
    monkeypatch.setattr(mgr, "_list_active_keys", lambda p: keys)
    # Patch _provider_repo.require_by_id to return the provider
    monkeypatch.setattr(
        mgr._provider_repo, "require_by_id", lambda pid_arg: provider
    )

    lease1 = mgr.acquire_key(pid)
    lease2 = mgr.acquire_key(pid)
    lease3 = mgr.acquire_key(pid)

    chosen = [lease1.key_id, lease2.key_id, lease3.key_id]
    k1_count = sum(1 for c in chosen if c == keys[0].id)
    k2_count = sum(1 for c in chosen if c == keys[1].id)
    assert k1_count == 2
    assert k2_count == 1


def test_key_pool_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid4()
    provider = _make_provider(pid)

    session = _FakeSession()
    mgr = KeyPoolManager(session)

    monkeypatch.setattr(mgr, "_list_active_keys", lambda p: [])
    monkeypatch.setattr(
        mgr._provider_repo, "require_by_id", lambda pid_arg: provider
    )

    with pytest.raises(ProviderError, match="No active keys"):
        mgr.acquire_key(pid)


def test_key_pool_release_success(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid4()
    key = _make_key(pid, weight=1, label="k1")
    provider = _make_provider(pid)

    session = _FakeSession()
    mgr = KeyPoolManager(session)

    monkeypatch.setattr(mgr, "_list_active_keys", lambda p: [key])
    monkeypatch.setattr(
        mgr._provider_repo, "require_by_id", lambda pid_arg: provider
    )
    # Allow get_by_id to return our key
    monkeypatch.setattr(
        mgr._key_repo, "get_by_id", lambda key_id: key if key_id == key.id else None
    )

    lease = KeyLease(
        key_id=key.id,
        provider_type="openai",
        provider_slug="oai",
        base_url=None,
        provider_kwargs={"api_key": "sk-k1"},
    )
    mgr.release_key(lease, success=True)
    assert key.consecutive_failures == 0


def test_key_pool_release_increments_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid4()
    key = _make_key(pid, weight=1, label="k1")
    key.consecutive_failures = 4  # one more → disabled

    session = _FakeSession()
    mgr = KeyPoolManager(session)

    monkeypatch.setattr(
        mgr._key_repo, "get_by_id", lambda key_id: key if key_id == key.id else None
    )

    lease = KeyLease(
        key_id=key.id,
        provider_type="openai",
        provider_slug="oai",
        base_url=None,
        provider_kwargs={"api_key": "sk-k1"},
    )
    mgr.release_key(lease, success=False)
    assert key.consecutive_failures == 5
    assert key.is_active is False


# ---------------------------------------------------------------------------
# ModelRouter — resolve flow via monkeypatching
# ---------------------------------------------------------------------------


def _fake_resolve_route(pid: UUID, profile_id: UUID) -> ResolvedRoute:
    provider = StubProvider(api_key="sk-test")
    return ResolvedRoute(
        provider=provider,
        provider_slug="openai",
        model_name="gpt-4",
        parameters={"temperature": 0.7},
        profile_id=str(profile_id),
    )


def test_model_router_resolve_chat_route(monkeypatch: pytest.MonkeyPatch) -> None:
    router = ModelRouter(_FakeSession())  # type: ignore[arg-type]
    profile_id = uuid4()

    monkeypatch.setattr(
        router,
        "resolve",
        lambda tenant_ctx, use_case, profile_override=None: _fake_resolve_route(
            uuid4(), profile_id
        ),
    )

    route = router.resolve(_ctx(), "chat")
    assert route.provider_slug == "openai"
    assert route.model_name == "gpt-4"
    assert route.parameters == {"temperature": 0.7}


def test_model_router_chat_returns_stub_result(monkeypatch: pytest.MonkeyPatch) -> None:
    router = ModelRouter(_FakeSession())  # type: ignore[arg-type]
    profile_id = uuid4()

    monkeypatch.setattr(
        router,
        "resolve",
        lambda tenant_ctx, use_case, profile_override=None: _fake_resolve_route(
            uuid4(), profile_id
        ),
    )

    result = router.chat(_ctx(), messages=[{"role": "user", "content": "hi"}])
    assert result.model == "gpt-4"


def test_model_router_embed_returns_stub_result(monkeypatch: pytest.MonkeyPatch) -> None:
    router = ModelRouter(_FakeSession())  # type: ignore[arg-type]
    profile_id = uuid4()

    monkeypatch.setattr(
        router,
        "resolve",
        lambda tenant_ctx, use_case, profile_override=None: _fake_resolve_route(
            uuid4(), profile_id
        ),
    )

    result = router.embed(_ctx(), texts=["alpha", "beta"])
    assert result.model == "gpt-4"
    assert len(result.embeddings) == 2
