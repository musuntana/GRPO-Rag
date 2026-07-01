from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.storage.models.app import TenantModelBinding
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import model_bindings as model_bindings_route

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


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.deleted: list = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.committed = True

    def begin(self) -> _FakeSession:
        return self

    def delete(self, obj) -> None:
        self.deleted.append(obj)


class _FakeBindingRepo:
    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def list(self, *, limit: int = 100, offset: int = 0) -> list:
        return [
            TenantModelBinding(
                id=uuid4(), tenant_id=UUID(TENANT_ID), use_case="chat",
                profile_id=uuid4(), is_default=True,
            )
        ]

    def upsert(self, *, use_case: str, profile_id: UUID,
               is_default: bool = False) -> TenantModelBinding:
        return TenantModelBinding(
            id=uuid4(), tenant_id=UUID(TENANT_ID), use_case=use_case,
            profile_id=profile_id, is_default=is_default,
        )

    def require_by_id(self, binding_id: UUID) -> TenantModelBinding:
        return TenantModelBinding(
            id=binding_id, tenant_id=UUID(TENANT_ID), use_case="chat",
            profile_id=uuid4(), is_default=False,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(model_bindings_route, "TenantModelBindingRepository", _FakeBindingRepo)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


def test_bindings_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get("/tenant/model-bindings")
    assert resp.status_code == 401


def test_bindings_rejects_non_tenant_admin(client: TestClient) -> None:
    resp = client.get("/tenant/model-bindings", headers=_headers(["end_user"]))
    assert resp.status_code == 403


def test_upsert_binding(client: TestClient) -> None:
    resp = client.put(
        "/tenant/model-bindings",
        headers=_headers(["tenant_admin"]),
        json={"use_case": "chat", "profile_id": str(uuid4()), "is_default": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["use_case"] == "chat"
    assert body["is_default"] is True


def test_list_bindings(client: TestClient) -> None:
    resp = client.get("/tenant/model-bindings", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


def test_delete_binding(client: TestClient) -> None:
    resp = client.delete(
        f"/tenant/model-bindings/{uuid4()}",
        headers=_headers(["tenant_admin"]),
    )
    assert resp.status_code == 204
