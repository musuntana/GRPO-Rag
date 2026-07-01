from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token

# Route module needed for the middleware to register /tenant/context.
from ragos_api.main import create_app

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"


def _token(roles: list[str]) -> str:
    return encode_dev_token(
        {
            "sub": "u_admin",
            "tenant_id": TENANT_ID,
            "tenant_slug": "acme",
            "actor_type": "tenant_admin" if "tenant_admin" in roles else "end_user",
            "roles": roles,
        }
    )


def _headers(roles: list[str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token(roles)}",
        "X-Tenant-Id": TENANT_ID,
    }


class _FakeSessionCtx:
    """Captures whether SET LOCAL was called and with what value.

    Provides the minimal surface area needed by both the middleware
    (via resolve_tenant_context → domain lookup) and handlers
    (via TenantRepository → require_by_id/get_by_id/etc).
    """

    def __init__(self) -> None:
        self.committed = False
        self.executed_rls: bool = False
        self.rls_value: str | None = None

    def __enter__(self) -> _FakeSessionCtx:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.committed = True

    def begin(self) -> _FakeSessionCtx:
        return self

    def execute(self, stmt, params=None) -> None:
        if "set_config" in str(stmt) and "current_tenant_id" in str(stmt):
            self.executed_rls = True
            if params:
                self.rls_value = params.get("tid")

    def exec(self, stmt):
        """Stand-in for sqlmodel.Session.exec() used by repositories."""
        return _FakeResult()


class _FakeResult:
    """Minimal exec() return value that supports .first() and .all()."""

    def first(self):
        return None

    def all(self):
        return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test app with fake session capturing RLS calls."""
    import ragos.storage.db as db_module
    import ragos_api.dependencies as deps

    handler_session = _FakeSessionCtx()
    middleware_session = _FakeSessionCtx()

    monkeypatch.setattr(db_module, "get_session_factory", lambda: middleware_session)

    def _override_db_session(request):
        yield handler_session

    app = create_app(testing=True)
    app.dependency_overrides[deps.get_db_session] = _override_db_session
    return TestClient(app)


def test_tenant_route_sets_rls_tenant_id_on_state(client: TestClient) -> None:
    """After a /tenant/* request, the middleware should have set rls_tenant_id."""
    resp = client.get("/tenant/context", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_slug"] == "acme"


def test_public_endpoint_no_auth(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
