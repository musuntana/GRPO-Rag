from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError, NotFoundError
from ragos.storage.models.app import Tenant
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import platform as platform_route


def _token(roles: list[str]) -> str:
    return encode_dev_token(
        {
            "sub": "u_admin",
            "actor_type": "platform_admin" if "platform_admin" in roles else "tenant_admin",
            "roles": roles,
        }
    )


def _headers(roles: list[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(roles)}"}


class _FakeSession:
    """Stand-in for the real sqlmodel Session in route-level tests."""

    def __init__(self) -> None:
        self.committed = False

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.committed = True

    def begin(self) -> _FakeSession:
        return self


class _FakeRepo:
    instances: list[_FakeRepo] = []

    def __init__(self, session: Any) -> None:
        self.session = session
        self.calls: list[tuple[str, tuple, dict]] = []
        _FakeRepo.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()

    def list_all(self, *, limit: int, offset: int) -> list[Tenant]:
        self.calls.append(("list_all", (), {"limit": limit, "offset": offset}))
        return [
            Tenant(id=UUID("11111111-1111-1111-1111-111111111111"), slug="a", display_name="A"),
        ]

    def create(self, *, slug: str, display_name: str) -> Tenant:
        self.calls.append(("create", (), {"slug": slug, "display_name": display_name}))
        if slug == "dup":
            raise ConflictError("Tenant slug already exists", slug=slug)
        return Tenant(id=uuid4(), slug=slug, display_name=display_name)

    def require_by_id(self, tenant_id: UUID) -> Tenant:
        self.calls.append(("require_by_id", (tenant_id,), {}))
        if str(tenant_id) == "00000000-0000-0000-0000-000000000000":
            raise NotFoundError("Tenant not found", resource="Tenant", id=str(tenant_id))
        return Tenant(id=tenant_id, slug="existing", display_name="Existing")

    def update(
        self,
        tenant_id: UUID,
        *,
        display_name: str | None = None,
        is_active: bool | None = None,
    ) -> Tenant:
        self.calls.append(
            (
                "update",
                (tenant_id,),
                {"display_name": display_name, "is_active": is_active},
            )
        )
        return Tenant(
            id=tenant_id,
            slug="existing",
            display_name=display_name or "Existing",
            is_active=is_active if is_active is not None else True,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakeRepo.reset()
    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    monkeypatch.setattr(platform_route, "PlatformTenantRepository", _FakeRepo)
    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


def test_platform_route_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get("/platform/tenants")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_platform_route_rejects_non_platform_admin(client: TestClient) -> None:
    resp = client.get("/platform/tenants", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


def test_platform_route_does_not_require_tenant_header(client: TestClient) -> None:
    # Platform calls must succeed without X-Tenant-Id — /platform/* is
    # tenant-less by design.
    resp = client.get("/platform/tenants", headers=_headers(["platform_admin"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["items"][0]["slug"] == "a"


def test_platform_route_create_tenant(client: TestClient) -> None:
    resp = client.post(
        "/platform/tenants",
        headers=_headers(["platform_admin"]),
        json={"slug": "acme2", "display_name": "Acme 2"},
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "acme2"


def test_platform_route_create_conflict(client: TestClient) -> None:
    resp = client.post(
        "/platform/tenants",
        headers=_headers(["platform_admin"]),
        json={"slug": "dup", "display_name": "Dup"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_platform_route_get_missing(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    resp = client.get(f"/platform/tenants/{missing}", headers=_headers(["platform_admin"]))
    assert resp.status_code == 404


def test_platform_route_deactivate(client: TestClient) -> None:
    tid = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        f"/platform/tenants/{tid}/deactivate",
        headers=_headers(["platform_admin"]),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
