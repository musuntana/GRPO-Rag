from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError, NotFoundError
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import platform_users as platform_users_route

MISSING_ID = "00000000-0000-0000-0000-000000000000"


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
    def __init__(self) -> None:
        self.committed = False

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.committed = True

    def begin(self) -> _FakeSession:
        return self


class _FakePlatformUserRepo:
    conflict_external_ids: set[tuple[UUID, str]] = set()

    def __init__(self, session: Any) -> None:
        self.session = session

    def list_for_tenant(self, tenant_id: UUID, *, limit: int, offset: int) -> list:
        return []

    def create(
        self, *, tenant_id: UUID, external_id: str, display_name: str,
        email: str | None = None, roles: list[str] | None = None,
    ):
        if (tenant_id, external_id) in _FakePlatformUserRepo.conflict_external_ids:
            raise ConflictError(
                "User external_id already exists in this tenant",
                external_id=external_id,
                tenant_id=str(tenant_id),
            )
        from ragos.storage.models.app import User

        return User(
            id=uuid4(),
            tenant_id=tenant_id,
            external_id=external_id,
            display_name=display_name,
            email=email,
            roles=roles or [],
        )

    def require_by_id(self, user_id: UUID):
        if str(user_id) == MISSING_ID:
            raise NotFoundError("User not found", resource="User", id=str(user_id))
        from ragos.storage.models.app import User

        return User(
            id=user_id,
            tenant_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            external_id="u1",
            display_name="Test User",
            email=None,
            roles=[],
        )

    def add(self, user) -> None:
        pass


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakePlatformUserRepo.conflict_external_ids = set()
    monkeypatch.setattr(platform_users_route, "PlatformUserRepository", _FakePlatformUserRepo)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


TID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_platform_user_route_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get(f"/platform/tenants/{TID}/users")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_platform_user_route_rejects_non_platform_admin(client: TestClient) -> None:
    resp = client.get(f"/platform/tenants/{TID}/users", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


def test_platform_user_route_list(client: TestClient) -> None:
    resp = client.get(f"/platform/tenants/{TID}/users", headers=_headers(["platform_admin"]))
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_platform_user_route_create(client: TestClient) -> None:
    resp = client.post(
        f"/platform/tenants/{TID}/users",
        headers=_headers(["platform_admin"]),
        json={"external_id": "ext-1", "display_name": "Alice"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["external_id"] == "ext-1"
    assert body["display_name"] == "Alice"


def test_platform_user_route_create_conflict(client: TestClient) -> None:
    _FakePlatformUserRepo.conflict_external_ids = {(UUID(TID), "dup")}
    resp = client.post(
        f"/platform/tenants/{TID}/users",
        headers=_headers(["platform_admin"]),
        json={"external_id": "dup", "display_name": "Dup"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_platform_user_route_get_not_found(client: TestClient) -> None:
    resp = client.get(
        f"/platform/tenants/{TID}/users/{MISSING_ID}",
        headers=_headers(["platform_admin"]),
    )
    assert resp.status_code == 404


def test_platform_user_route_deactivate(client: TestClient) -> None:
    uid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    resp = client.delete(
        f"/platform/tenants/{TID}/users/{uid}",
        headers=_headers(["platform_admin"]),
    )
    assert resp.status_code == 204
