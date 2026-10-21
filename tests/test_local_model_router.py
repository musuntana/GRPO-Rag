"""Dev-only local ModelRouter admission, authorization and real wire contract.

No Qwen weights are called by this test suite. The mock HTTP transport is
deliberately attached *after* a tenant-bound model profile is resolved.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from ragos.core.config import Settings
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.local_admission import (
    LocalModelAdmission,
    LocalPinnedProvider,
    local_admission_from_settings,
)
from ragos.model_gateway.router import ModelRouter
from ragos.storage.models.app import ModelProfile, Provider
from test_model_gateway import TENANT_ID, _FakeSession, _ctx

BASE = "http://127.0.0.1:12434/engines/v1"
CHAT = "ai/qwen3:4b-instruct"
EMBED = "ragos/qwen3-embedding:0.6b-q8"


class LocalWire:
    def __init__(self, *, dimension=3):
        self.calls = []
        self.dimension = dimension

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        if url.endswith("/chat/completions"):
            return {
                "choices": [{"message": {"content": "Synthetic answer [1]."}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        if url.endswith("/embeddings"):
            return {
                "data": [
                    {"index": i, "embedding": [1.0] + [0.0] * (self.dimension - 1)}
                    for i in range(len(payload["input"]))
                ]
            }
        raise AssertionError("Unexpected provider route")


def _admission(*, wire=None) -> LocalModelAdmission:
    return LocalModelAdmission(
        provider_id=uuid4(), base_url=BASE,
        chat_model=CHAT, embedding_model=EMBED,
        embedding_dimension=3, transport=wire,
    )


def _router(monkeypatch, *, case="chat", config=None, model=None, provider=None):
    monkeypatch.setattr(
        "ragos.model_gateway.router.get_settings",
        lambda: Settings(_env_file=None, app_env="dev", api_host="127.0.0.1"),
    )
    config = config or _admission(wire=LocalWire())
    router = ModelRouter(_FakeSession(), local_model=config)
    profile = ModelProfile(
        id=uuid4(), tenant_id=TENANT_ID, provider_id=config.provider_id,
        name="dev-model-profile", use_case=case,
        model_name=model or config.model_for(case),
    )
    provider = provider or Provider(
        id=config.provider_id, slug="local-dmr",
        provider_type="local_openai", display_name="Local Model Runner",
        base_url=config.base_url,
    )
    monkeypatch.setattr(router._profiles, "resolve", lambda **kwargs: profile)
    monkeypatch.setattr(
        router._keys._provider_repo, "get_by_id",
        lambda provider_id: provider if provider.id == provider_id else None,
    )
    def forbid_key_read(*args, **kwargs):
        raise AssertionError("local endpoint must never read encrypted DB keys")

    monkeypatch.setattr(router._keys, "_list_active_keys", forbid_key_read)
    return router


def test_local_gateway_disabled_by_default_and_rejected_outside_dev() -> None:
    disabled = Settings(
        _env_file=None, app_env="dev",
        local_model_enabled=False, local_model_provider_id=None,
    )
    assert local_admission_from_settings(disabled) is None
    for app_env in ("test", "staging", "prod"):
        bad = Settings(
            _env_file=None, app_env=app_env, local_model_enabled=True,
            local_model_provider_id=uuid4(), local_model_base_url=BASE,
            local_model_chat=CHAT, local_model_embed=EMBED,
            local_model_embed_dimension=3,
        )
        with pytest.raises(ValidationError, match="development"):
            local_admission_from_settings(bad)


def test_explicit_dev_config_requires_pins() -> None:
    settings = Settings(
        _env_file=None, app_env="dev", api_host="127.0.0.1",
        local_model_enabled=True,
        local_model_provider_id=uuid4(),
        local_model_base_url=BASE,
        local_model_chat=CHAT, local_model_embed=EMBED,
        local_model_embed_dimension=3,
    )
    c = local_admission_from_settings(settings)
    assert c is not None
    assert c.embedding_dimension == 3
    assert c.model_for("chat") == CHAT
    assert c.model_for("embed") == EMBED
    settings.api_host = "0.0.0.0"
    with pytest.raises(ValidationError, match="loopback"):
        local_admission_from_settings(settings)
    with pytest.raises(ProviderError):
        c.model_for("rerank")


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://models.example.com/v1",
        "http://10.0.0.1:12434/v1",
        "http://user:pw@127.0.0.1:12434/v1",
        "http://127.0.0.1:12434/v1?token=abc",
        "http://127.0.0.1:12434/chat",
        "file:///etc/passwd",
    ],
)
def test_never_admit_remote_or_credentialed_urls(bad_url) -> None:
    with pytest.raises(ValidationError, match="local HTTP"):
        replace(_admission(), base_url=bad_url)


def test_authorized_chat_routes_through_real_http_adapter_without_db_key(monkeypatch) -> None:
    wire = LocalWire()
    router = _router(monkeypatch, config=_admission(wire=wire))
    result = router.chat(_ctx(), messages=[{"role": "user", "content": "synthetic hi"}])
    assert result.content == "Synthetic answer [1]."
    assert len(wire.calls) == 1
    url, payload, headers, timeout = wire.calls[0]
    assert url == BASE + "/chat/completions"
    assert payload["model"] == CHAT
    assert payload["max_tokens"] == 256
    assert payload["stream"] is False
    assert "Authorization" not in headers
    assert timeout == 30


def test_authorized_embedding_is_pinned_and_dimension_checked(monkeypatch) -> None:
    wire = LocalWire()
    router = _router(monkeypatch, case="embed", config=_admission(wire=wire))
    result = router.embed(_ctx(), texts=["synthetic contract page"])
    assert len(result.embeddings[0]) == 3
    assert wire.calls[0][0] == BASE + "/embeddings"
    assert "Authorization" not in wire.calls[0][2]
    bad = _router(
        monkeypatch, case="embed", config=_admission(wire=LocalWire(dimension=2))
    )
    with pytest.raises(ProviderError, match="dimension"):
        bad.embed(_ctx(), texts=["synthetic contract page"])


def test_wrong_model_or_platform_provider_denied_without_http_or_key(monkeypatch) -> None:
    wire = LocalWire()
    config = _admission(wire=wire)
    wrong_model = _router(monkeypatch, config=config, model="other-chat")
    with pytest.raises(ProviderError, match="not admitted"):
        wrong_model.chat(_ctx(), messages=[{"role": "user", "content": "hello"}])

    bad_provider = Provider(
        id=config.provider_id, slug="not-local",
        provider_type="openai", display_name="Remote API", base_url=BASE,
    )
    unauthorized = _router(monkeypatch, config=config, provider=bad_provider)
    with pytest.raises(ProviderError, match="unavailable"):
        unauthorized.chat(_ctx(), messages=[{"role": "user", "content": "hello"}])
    assert not wire.calls


def test_local_provider_rejects_unbounded_inputs_and_reranking() -> None:
    model = LocalPinnedProvider(_admission(wire=LocalWire()))
    with pytest.raises(ValidationError, match="max_tokens"):
        model.chat(
            model=CHAT, messages=[{"role": "user", "content": "hello"}],
            parameters={"max_tokens": 10000},
        )
    with pytest.raises(ValidationError, match="limits"):
        model.embed(model=EMBED, texts=["x" * 9000])
    with pytest.raises(ProviderError, match="not admitted"):
        model.rerank(model=CHAT, query="q", documents=["page"])


def test_model_router_automatically_enables_only_explicit_local_settings(
    monkeypatch,
) -> None:
    config = _admission()
    enabled = Settings(
        _env_file=None, app_env="dev", api_host="127.0.0.1",
        local_model_enabled=True, local_model_provider_id=config.provider_id,
        local_model_base_url=BASE, local_model_chat=CHAT,
        local_model_embed=EMBED, local_model_embed_dimension=3,
    )
    monkeypatch.setattr(
        "ragos.model_gateway.router.get_settings", lambda: enabled
    )
    router = ModelRouter(_FakeSession())
    assert router._local is not None
    assert router._local.provider_id == config.provider_id

    disabled = enabled.model_copy(update={"local_model_enabled": False})
    monkeypatch.setattr(
        "ragos.model_gateway.router.get_settings", lambda: disabled
    )
    assert ModelRouter(_FakeSession())._local is None


def test_injected_local_model_rejected_on_exposed_dev_api_or_prod(monkeypatch) -> None:
    local = _admission()
    for app_env, api_host in (
        ("dev", "0.0.0.0"),
        ("prod", "127.0.0.1"),
    ):
        monkeypatch.setattr(
            "ragos.model_gateway.router.get_settings",
            lambda env=app_env, host=api_host: Settings(
                _env_file=None, app_env=env, api_host=host
            ),
        )
        with pytest.raises(ValidationError, match="local model gateway"):
            ModelRouter(_FakeSession(), local_model=local)
