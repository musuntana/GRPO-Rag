from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError
from ragos.storage.models.app import KeyPool, ModelProfile, Provider
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import model_center as model_center_route

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"


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


class _FakeProviderRepo:
    conflict_slugs: set[str] = set()

    def __init__(self, session: Any) -> None:
        self.session = session

    def list(self, *, limit: int, offset: int) -> list:
        return [Provider(id=uuid4(), slug="openai", provider_type="openai",
                        display_name="OpenAI")]

    def create(self, *, slug: str, provider_type: str, display_name: str,
               base_url: str | None = None) -> Provider:
        if slug in _FakeProviderRepo.conflict_slugs:
            raise ConflictError("Provider slug already exists", slug=slug)
        return Provider(id=uuid4(), slug=slug, provider_type=provider_type,
                        display_name=display_name, base_url=base_url)

    def require_by_id(self, provider_id: UUID) -> Provider:
        return Provider(id=provider_id, slug="oai", provider_type="openai",
                        display_name="OpenAI")


class _FakeKeyPoolRepo:
    def __init__(self, session: Any) -> None:
        self.session = session

    def list_for_provider(self, provider_id: UUID) -> list:
        return [KeyPool(id=uuid4(), provider_id=provider_id, label="k1",
                        api_key_encrypted="sk-xxx", weight=1)]


class _FakeProfileRepo:
    def __init__(self, session: Any) -> None:
        self.session = session

    def list(self, *, limit: int, offset: int, tenant_id: UUID | None = None) -> list:
        return [ModelProfile(id=uuid4(), tenant_id=TENANT_ID, name="gpt4",
                             use_case="chat", provider_id=uuid4(),
                             model_name="gpt-4", parameters={})]

    def create(self, *, tenant_id: UUID, name: str, use_case: str, provider_id: UUID,
               model_name: str, parameters: dict | None = None,
               fallback_profile_id: UUID | None = None) -> ModelProfile:
        return ModelProfile(id=uuid4(), tenant_id=tenant_id, name=name,
                            use_case=use_case, provider_id=provider_id,
                            model_name=model_name, parameters=parameters or {})

    def require_by_id(self, profile_id: UUID) -> ModelProfile:
        return ModelProfile(id=profile_id, tenant_id=TENANT_ID, name="gpt4",
                            use_case="chat", provider_id=uuid4(),
                            model_name="gpt-4", parameters={})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakeProviderRepo.conflict_slugs = set()
    monkeypatch.setattr(model_center_route, "ProviderRepository", _FakeProviderRepo)
    monkeypatch.setattr(model_center_route, "KeyPoolRepository", _FakeKeyPoolRepo)
    monkeypatch.setattr(model_center_route, "PlatformModelProfileRepository", _FakeProfileRepo)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app)


# -- Auth --


def test_model_center_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get("/platform/model-center/providers")
    assert resp.status_code == 401


def test_model_center_rejects_non_platform_admin(client: TestClient) -> None:
    resp = client.get("/platform/model-center/providers", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 403


# -- Providers --


def test_list_providers(client: TestClient) -> None:
    resp = client.get("/platform/model-center/providers", headers=_headers(["platform_admin"]))
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_create_provider(client: TestClient) -> None:
    resp = client.post(
        "/platform/model-center/providers",
        headers=_headers(["platform_admin"]),
        json={"slug": "anthropic", "provider_type": "anthropic", "display_name": "Anthropic"},
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "anthropic"


def test_create_provider_conflict(client: TestClient) -> None:
    _FakeProviderRepo.conflict_slugs = {"dup"}
    resp = client.post(
        "/platform/model-center/providers",
        headers=_headers(["platform_admin"]),
        json={"slug": "dup", "provider_type": "openai", "display_name": "Dup"},
    )
    assert resp.status_code == 409


# -- Key Pools --


def test_list_key_pools(client: TestClient) -> None:
    pid = str(uuid4())
    resp = client.get(
        f"/platform/model-center/providers/{pid}/key-pools",
        headers=_headers(["platform_admin"]),
    )
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_create_key_pool_rejects_raw_credential_intake(client: TestClient) -> None:
    pid = str(uuid4())
    resp = client.post(
        f"/platform/model-center/providers/{pid}/key-pools",
        headers=_headers(["platform_admin"]),
        json={"label": "primary", "api_key": "sk-test", "weight": 2},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "sk-test" not in resp.text


# -- Profiles --


def test_create_profile(client: TestClient) -> None:
    pid = str(uuid4())
    resp = client.post(
        "/platform/model-center/profiles",
        headers=_headers(["platform_admin"]),
        json={
            "tenant_id": TENANT_ID,
            "name": "GPT-4 Chat",
            "use_case": "chat",
            "provider_id": pid,
            "model_name": "gpt-4",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["model_name"] == "gpt-4"


def test_list_profiles(client: TestClient) -> None:
    resp = client.get(
        "/platform/model-center/profiles",
        headers=_headers(["platform_admin"]),
    )
    assert resp.status_code == 200
    assert "items" in resp.json()
