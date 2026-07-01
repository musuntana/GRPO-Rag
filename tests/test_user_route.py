from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError, NotFoundError
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import users as users_route

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"
MISSING_ID = "00000000-0000-0000-0000-000000000000"


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


class _FakeUserRepo:
    conflict_external_ids: set[str] = set()

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def list(self, *, limit: int, offset: int) -> list:
        from ragos.storage.models.app import User

        return [
            User(
                id=UUID("11111111-1111-1111-1111-111111111111"),
                tenant_id=UUID(TENANT_ID),
                external_id="u1",
                display_name="Alice",
            )
        ]

    def create(
        self, *, external_id: str, display_name: str,
        email: str | None = None, roles: list[str] | None = None,
    ):
        if external_id in _FakeUserRepo.conflict_external_ids:
            raise ConflictError(
                "User external_id already exists in this tenant",
                external_id=external_id,
            )
        from ragos.storage.models.app import User

        return User(
            id=uuid4(),
            tenant_id=UUID(TENANT_ID),
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
            tenant_id=UUID(TENANT_ID),
            external_id="u1",
            display_name="Test User",
        )

    def update(
        self, user_id: UUID, *,
        display_name: str | None = None,
        email: str | None = None,
        roles: list[str] | None = None,
        is_active: bool | None = None,
    ):
        user = self.require_by_id(user_id)
        if display_name is not None:
            user.display_name = display_name
        if is_active is not None:
            user.is_active = is_active
        return user


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakeUserRepo.conflict_external_ids = set()
    monkeypatch.setattr(users_route, "UserRepository", _FakeUserRepo)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


def test_user_route_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get("/tenant/users")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_user_route_rejects_non_tenant_admin(client: TestClient) -> None:
    resp = client.get("/tenant/users", headers=_headers(["end_user"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


def test_user_route_list_returns_items(client: TestClient) -> None:
    resp = client.get("/tenant/users", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["external_id"] == "u1"


def test_user_route_create(client: TestClient) -> None:
    resp = client.post(
        "/tenant/users",
        headers=_headers(["tenant_admin"]),
        json={"external_id": "ext-2", "display_name": "Bob"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["external_id"] == "ext-2"
    assert body["display_name"] == "Bob"


def test_user_route_create_conflict(client: TestClient) -> None:
    _FakeUserRepo.conflict_external_ids = {"dup"}
    resp = client.post(
        "/tenant/users",
        headers=_headers(["tenant_admin"]),
        json={"external_id": "dup", "display_name": "Dup"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_user_route_get_not_found(client: TestClient) -> None:
    resp = client.get(
        f"/tenant/users/{MISSING_ID}",
        headers=_headers(["tenant_admin"]),
    )
    assert resp.status_code == 404


def test_user_route_deactivate(client: TestClient) -> None:
    uid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    resp = client.delete(
        f"/tenant/users/{uid}",
        headers=_headers(["tenant_admin"]),
    )
    assert resp.status_code == 204
