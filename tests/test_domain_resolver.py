from __future__ import annotations

from uuid import uuid4

import pytest
from ragos.auth.jwt import AuthClaims
from ragos.core.errors import ValidationError
from ragos.core.tenant import ActorType
from ragos.storage.models.app import TenantDomain
from ragos.tenant.resolver import resolve_tenant_context


class _StubRequest:
    """Minimal Request stand-in for resolver unit tests."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _StubSession:
    """Minimal Session stand-in with an exec method that returns a callable for first()."""

    results: list = []

    def exec(self, stmt) -> _StubSession:
        return self

    def first(self):
        return _StubSession.results[0] if _StubSession.results else None


def _claims(
    *,
    tenant_id: str | None = None,
    tenant_slug: str | None = None,
    roles: list[str] | None = None,
) -> AuthClaims:
    return AuthClaims(
        sub="u_1",
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        actor_type=ActorType.TENANT_ADMIN,
        roles=roles or ["tenant_admin"],
    )


def test_resolver_uses_x_tenant_id_header() -> None:
    request = _StubRequest({"X-Tenant-Id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})
    ctx = resolve_tenant_context(request, _claims(), trace_id="t-1")
    assert str(ctx.tenant_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert ctx.tenant_slug == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # fallback


def test_resolver_uses_x_tenant_slug_header() -> None:
    request = _StubRequest(
        {
            "X-Tenant-Id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "X-Tenant-Slug": "acme",
        }
    )
    ctx = resolve_tenant_context(request, _claims(), trace_id="t-1")
    assert ctx.tenant_slug == "acme"


def test_resolver_uses_jwt_claim_when_no_header() -> None:
    request = _StubRequest({})
    ctx = resolve_tenant_context(
        request,
        _claims(tenant_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", tenant_slug="beta"),
        trace_id="t-2",
    )
    assert str(ctx.tenant_id) == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_resolver_header_overrides_jwt() -> None:
    request = _StubRequest({"X-Tenant-Id": "cccccccc-cccc-cccc-cccc-cccccccccccc"})
    ctx = resolve_tenant_context(
        request,
        _claims(tenant_id="dddddddd-dddd-dddd-dddd-dddddddddddd"),
        trace_id="t-3",
    )
    assert str(ctx.tenant_id) == "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_resolver_raises_when_no_identifier() -> None:
    request = _StubRequest({})
    claims = _claims(tenant_id=None)
    with pytest.raises(ValidationError, match="Missing tenant identifier"):
        resolve_tenant_context(request, claims, trace_id="t-4")


def test_resolver_falls_back_to_slug_from_jwt_when_no_header_slug() -> None:
    request = _StubRequest({"X-Tenant-Id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"})
    ctx = resolve_tenant_context(
        request,
        _claims(tenant_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", tenant_slug="gamma"),
        trace_id="t-5",
    )
    assert ctx.tenant_slug == "gamma"


def test_domain_resolution_derives_tenant_from_host_header() -> None:
    tenant_id = uuid4()
    domain_record = TenantDomain(
        id=uuid4(),
        tenant_id=tenant_id,
        domain="chat.acme.com",
        is_verified=True,
    )
    _StubSession.results = [domain_record]

    request = _StubRequest({"Host": "chat.acme.com:8000"})
    claims = _claims(tenant_id=None)

    ctx = resolve_tenant_context(
        request, claims, trace_id="t-6", session=_StubSession()  # type: ignore[arg-type]
    )
    assert ctx.tenant_id == tenant_id

    _StubSession.results = []


def test_domain_resolution_requires_verified_domain() -> None:
    """Unverified domain → resolver query filters is_verified=True, so stub
    must return None to simulate the DB filtering out the unverified record."""
    request = _StubRequest({"Host": "unverified.acme.com"})
    claims = _claims(tenant_id=None)

    # resolver queries: WHERE domain = X AND is_verified = True
    # An unverified record would NOT match. Simulate that with empty results.
    _StubSession.results = []

    with pytest.raises(ValidationError, match="Missing tenant identifier"):
        resolve_tenant_context(
            request, claims, trace_id="t-7", session=_StubSession()  # type: ignore[arg-type]
        )

    _StubSession.results = []


def test_domain_resolution_not_called_when_x_tenant_id_present() -> None:
    """X-Tenant-Id takes precedence, so domain lookup should not happen."""
    request = _StubRequest(
        {
            "X-Tenant-Id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "Host": "chat.acme.com",
        }
    )
    claims = _claims(tenant_id=None)
    ctx = resolve_tenant_context(request, claims, trace_id="t-8")
    assert str(ctx.tenant_id) == "ffffffff-ffff-ffff-ffff-ffffffffffff"
