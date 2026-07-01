from __future__ import annotations

from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos_api.main import create_app


def auth_headers(
    *,
    roles: list[str],
    tenant_id: str = "fa9d4df0-81ce-4f1a-9820-4c29259779a6",
) -> dict[str, str]:
    token = encode_dev_token(
        {
            "sub": "u_123",
            "tenant_id": tenant_id,
            "tenant_slug": "acme",
            "actor_type": "tenant_admin" if "tenant_admin" in roles else "end_user",
            "roles": roles,
        }
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": tenant_id,
    }


def test_tenant_route_requires_authentication_and_returns_request_id() -> None:
    client = TestClient(create_app(testing=True))

    response = client.get("/tenant/context", headers={"X-Request-Id": "req-auth-1"})

    assert response.status_code == 401
    assert response.headers["X-Request-Id"] == "req-auth-1"
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_tenant_route_requires_tenant_admin_role() -> None:
    client = TestClient(create_app(testing=True))

    response = client.get("/tenant/context", headers=auth_headers(roles=["user"]))

    assert response.status_code == 403
    assert response.headers["X-Request-Id"]
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_tenant_route_resolves_context_from_middleware() -> None:
    client = TestClient(create_app(testing=True))

    response = client.get(
        "/tenant/context",
        headers=auth_headers(roles=["tenant_admin"]),
    )

    assert response.status_code == 200
    assert response.headers["X-Request-Id"]
    payload = response.json()
    assert payload["tenant_slug"] == "acme"
    assert payload["actor_id"] == "u_123"
