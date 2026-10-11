"""Unconnected providers must not manufacture successful stub model results."""

from uuid import uuid4

import pytest
from ragos.core.errors import ProviderError
from ragos.model_gateway.key_pool import KeyLease, KeyPoolManager


@pytest.mark.parametrize("kind", ["stub", "openai", "anthropic", "unknown"])
def test_default_provider_factory_refuses_all_unconnected_types(kind):
    lease = KeyLease(
        uuid4(),
        kind,
        "configured",
        "https://private.invalid",
        {"api_key": "encrypted-private-value"},
    )
    with pytest.raises(ProviderError) as failure:
        KeyPoolManager(None).build_provider(lease)
    assert "private" not in str(failure.value)


def test_explicit_test_opt_in_does_not_enable_real_or_unknown_types():
    with pytest.raises(ProviderError):
        KeyPoolManager(None, allow_test_stub=True).build_provider(
            KeyLease(uuid4(), "openai", "configured", None, {})
        )


def test_explicit_stub_receives_no_secret_or_network_configuration(monkeypatch):
    import ragos.model_gateway.providers.stub as module

    received = []
    monkeypatch.setattr(
        module, "StubProvider", lambda **kwargs: received.append(kwargs) or object()
    )
    KeyPoolManager(None, allow_test_stub=True).build_provider(
        KeyLease(uuid4(), "stub", "test", "https://private.invalid", {"api_key": "secret"})
    )
    assert received == [{"api_key": "test-only-placeholder"}]


def test_explicit_stub_constructs_the_actual_test_provider():
    provider = KeyPoolManager(None, allow_test_stub=True).build_provider(
        KeyLease(uuid4(), "stub", "test", None, {})
    )
    assert provider.chat(messages=[], model="test-only").content == ""
