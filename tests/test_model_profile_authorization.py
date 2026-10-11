from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from ragos.core.errors import NotFoundError, PermissionDeniedError
from ragos.core.tenant import ActorType, TenantContext
from ragos.model_gateway.profiles import ProfileResolver
from ragos.model_gateway.router import ModelRouter
from ragos.storage.models.app import ModelProfile, TenantModelBinding


class BindingSession:
    """Mock persistence which applies the actual query's tenant/use_case filters."""

    def __init__(self, bindings, profiles):
        self.bindings = bindings
        self.profiles = {profile.id: profile for profile in profiles}
        self.profile_reads = []

    def exec(self, statement):
        parameters = statement.compile().params
        tenant_id = parameters["tenant_id_1"]
        use_case = parameters["use_case_1"]
        binding = next(
            (
                item
                for item in self.bindings
                if item.tenant_id == tenant_id and item.use_case == use_case
            ),
            None,
        )
        return SimpleNamespace(first=lambda: binding)

    def get(self, model, profile_id):
        assert model is ModelProfile
        self.profile_reads.append(profile_id)
        return self.profiles.get(profile_id)


def context(tenant_id):
    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug="test",
        actor_id="actor",
        actor_type=ActorType.TENANT_ADMIN,
        roles=["tenant_admin"],
        trace_id="test",
    )


def profile(use_case="chat", *, active=True):
    return ModelProfile(
        id=uuid4(),
        name="test",
        use_case=use_case,
        provider_id=uuid4(),
        model_name="test-model",
        is_active=active,
    )


def binding(tenant_id, model, use_case="chat"):
    if model.tenant_id is None:
        model.tenant_id = tenant_id
    return TenantModelBinding(tenant_id=tenant_id, profile_id=model.id, use_case=use_case)


def test_misbound_foreign_profile_is_refused_even_with_privileged_storage():
    tenant_id = uuid4()
    model = profile()
    model.tenant_id = uuid4()
    session = BindingSession([binding(tenant_id, model)], [model])
    router = ModelRouter(session)
    router._keys = Mock()
    with pytest.raises(PermissionDeniedError, match="owner"):
        router.resolve(context(tenant_id), "chat")
    router._keys.acquire_key.assert_not_called()
    router._keys.build_provider.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_binding_authorizes_only_its_profile(explicit):
    tenant_id = uuid4()
    model = profile()
    session = BindingSession([binding(tenant_id, model)], [model])
    assert (
        ProfileResolver(session).resolve(
            tenant_id=tenant_id,
            use_case="chat",
            profile_override=model.id if explicit else None,
        )
        is model
    )
    assert session.profile_reads == [model.id]


@pytest.mark.parametrize("case", ["unbound", "other_tenant", "wrong_binding_use_case", "override"])
def test_denied_override_never_reads_requested_profile_or_acquires_key(case):
    tenant_id = uuid4()
    authorized = profile()
    requested = profile()
    bindings = []
    expected_error = NotFoundError
    if case == "other_tenant":
        bindings = [binding(uuid4(), requested)]
    elif case == "wrong_binding_use_case":
        bindings = [binding(tenant_id, requested, use_case="embed")]
    elif case == "override":
        bindings = [binding(tenant_id, authorized)]
        expected_error = PermissionDeniedError
    session = BindingSession(bindings, [authorized, requested])
    router = ModelRouter(session)
    router._keys = Mock()
    with pytest.raises(expected_error) as error:
        router.resolve(context(tenant_id), "chat", profile_override=requested.id)
    assert str(requested.id) not in str(error.value.to_response_content())
    assert session.profile_reads == []
    router._keys.acquire_key.assert_not_called()
    router._keys.build_provider.assert_not_called()


@pytest.mark.parametrize("case", ["inactive", "missing", "wrong_profile_use_case"])
def test_invalid_bound_profile_stops_before_provider_side_effects(case):
    tenant_id = uuid4()
    model = profile(
        "embed" if case == "wrong_profile_use_case" else "chat", active=case != "inactive"
    )
    session = BindingSession([binding(tenant_id, model)], [] if case == "missing" else [model])
    router = ModelRouter(session)
    router._keys = Mock()
    expected_error = PermissionDeniedError if case == "wrong_profile_use_case" else NotFoundError
    with pytest.raises(expected_error):
        router.chat(context(tenant_id), messages=[], profile_override=model.id)
    router._keys.acquire_key.assert_not_called()
    router._keys.build_provider.assert_not_called()


def test_authorized_route_resolves_before_key_and_provider():
    tenant_id = uuid4()
    model = profile()
    session = BindingSession([binding(tenant_id, model)], [model])
    router = ModelRouter(session)
    lease = SimpleNamespace(provider_slug="mock")
    provider = Mock()
    router._keys = Mock()

    def acquire(provider_id):
        assert session.profile_reads == [model.id]
        assert provider_id == model.provider_id
        return lease

    router._keys.acquire_key.side_effect = acquire
    router._keys.build_provider.return_value = provider
    route = router.resolve(context(tenant_id), "chat", profile_override=model.id)
    assert route.provider is provider
    assert route.profile_id == str(model.id)
    router._keys.acquire_key.assert_called_once_with(model.provider_id)
    router._keys.build_provider.assert_called_once_with(lease)
