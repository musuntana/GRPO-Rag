"""Provider admission precedes key acquisition for every authorized route.

The real ``ProfileResolver``, ``ModelRouter`` and ``KeyPoolManager`` run against a
temporary in-memory SQLite database built from the real SQLModel tables. Only the
dialect-specific DDL is adapted on a metadata copy. Statements are observed at the
engine; ``acquire_key`` is wrapped, not replaced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ragos.auth.jwt import decode_bearer_token, encode_dev_token
from ragos.core.errors import NotFoundError, PermissionDeniedError, ProviderError
from ragos.core.tenant import ActorType, TenantContext
from ragos.model_gateway.key_pool import KeyPoolManager
from ragos.model_gateway.providers.stub import StubProvider
from ragos.model_gateway.router import ModelRouter
from ragos.storage.models.app import (
    APP_SCHEMA,
    KeyPool,
    ModelProfile,
    Provider,
    Tenant,
    TenantModelBinding,
)
from ragos_api.dependencies import AuthCtx, DBSession, TenantCtx, get_db_session
from ragos_api.errors import register_error_handlers
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

UNAVAILABLE = "Model provider unavailable"
NO_KEY = "No active keys available for provider"
PROVIDER_URL = "https://internal-gateway.invalid/v1"
KEY_SECRET = "sk-s1-1-secret-value"
INACTIVE_KEY_SECRET = "sk-s1-1-inactive-value"
STUB_PLACEHOLDER = "test-only-placeholder"

KEY_POOL_SELECT = re.compile(r"FROM\s+(?:main\.)?key_pool\b")
PROVIDER_SELECT = re.compile(r"FROM\s+(?:main\.)?provider\b")


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


def _sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ).execution_options(schema_translate_map={APP_SCHEMA: None})
    metadata = MetaData()
    for model in (Tenant, Provider, KeyPool, ModelProfile, TenantModelBinding):
        model.__table__.to_metadata(metadata)
    for table in metadata.tables.values():
        for column in table.columns:
            column.server_default = None
    metadata.create_all(engine)
    return engine


@dataclass
class World:
    engine: object
    session: Session
    statements: list[str] = field(default_factory=list)
    acquire_calls: list[UUID] = field(default_factory=list)
    tenant_id: UUID = field(default_factory=uuid4)
    provider: Provider | None = None
    key: KeyPool | None = None
    inactive_key: KeyPool | None = None
    profile: ModelProfile | None = None

    def count(self, pattern: re.Pattern[str]) -> int:
        return sum(1 for statement in self.statements if pattern.search(statement))

    def context(self) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            tenant_slug="acme",
            actor_id="u_1",
            actor_type=ActorType.END_USER,
            roles=["end_user"],
            trace_id="s1-1-admission",
        )


def _build_world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_type: str = "stub",
    provider_active: bool = True,
    with_provider: bool = True,
    with_active_key: bool = True,
    binding: bool = True,
) -> World:
    engine = _sqlite_engine()
    session = Session(engine)
    world = World(engine=engine, session=session)

    provider_id = uuid4()
    if with_provider:
        world.provider = Provider(
            id=provider_id,
            slug="s1-provider",
            provider_type=provider_type,
            display_name="S1 provider",
            base_url=PROVIDER_URL,
            is_active=provider_active,
        )
        session.add(world.provider)
    world.key = KeyPool(
        id=uuid4(),
        provider_id=provider_id,
        label="primary",
        api_key_encrypted=KEY_SECRET,
        weight=1,
        is_active=with_active_key,
    )
    session.add(world.key)
    world.inactive_key = KeyPool(
        id=uuid4(),
        provider_id=provider_id,
        label="retired",
        api_key_encrypted=INACTIVE_KEY_SECRET,
        weight=1,
        is_active=False,
    )
    session.add(world.inactive_key)
    world.profile = ModelProfile(
        id=uuid4(),
        tenant_id=world.tenant_id,
        name="chat",
        use_case="chat",
        provider_id=provider_id,
        model_name="s1-test-model",
        is_active=True,
    )
    session.add(world.profile)
    if binding:
        session.add(
            TenantModelBinding(
                tenant_id=world.tenant_id, profile_id=world.profile.id, use_case="chat"
            )
        )
    session.commit()

    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: world.statements.append(
            statement
        ),
    )
    original = KeyPoolManager.acquire_key

    def spy(self, provider_id):
        world.acquire_calls.append(provider_id)
        return original(self, provider_id)

    monkeypatch.setattr(KeyPoolManager, "acquire_key", spy)
    return world


@pytest.fixture
def close_world():
    worlds: list[World] = []
    yield worlds
    for world in worlds:
        world.session.close()
        world.engine.dispose()


def _leak_free(text: str, world: World) -> None:
    assert str(world.provider.id) not in text
    assert str(world.key.id) not in text
    assert str(world.inactive_key.id) not in text
    assert PROVIDER_URL not in text
    assert KEY_SECRET not in text
    assert INACTIVE_KEY_SECRET not in text


# ---------------------------------------------------------------------------
# Case A: unavailable provider is refused before any key is read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["disabled", "unconnected_type", "stub_without_opt_in", "missing_provider"],
)
def test_unavailable_provider_is_refused_before_key_acquisition(monkeypatch, close_world, case):
    world = _build_world(
        monkeypatch,
        provider_active=case != "disabled",
        provider_type="openai" if case == "unconnected_type" else "stub",
        with_provider=case != "missing_provider",
    )
    close_world.append(world)
    router = ModelRouter(world.session, allow_test_stub=case != "stub_without_opt_in")
    if case == "missing_provider":
        world.session.delete(world.key)
        world.session.commit()
        world.key = None
        world.profile.provider_id = uuid4()
        world.session.commit()

    world.statements.clear()
    before = dict(router._keys._round_robin)
    with pytest.raises(ProviderError) as failure:
        router.resolve(world.context(), "chat")

    assert failure.value.status_code == 502
    assert failure.value.message == UNAVAILABLE
    assert failure.value.details == {}
    assert world.acquire_calls == []
    assert world.count(PROVIDER_SELECT) == 1
    assert world.count(KEY_POOL_SELECT) == 0
    assert router._keys._round_robin == before
    if world.provider is not None:
        _leak_free(str(failure.value.to_response_content()), world)


# ---------------------------------------------------------------------------
# Case B: admitted provider without an active key fails after the key query
# ---------------------------------------------------------------------------


def test_admitted_provider_without_key_is_generic_502(monkeypatch, close_world):
    world = _build_world(monkeypatch, with_active_key=False)
    close_world.append(world)
    router = ModelRouter(world.session, allow_test_stub=True)

    world.statements.clear()
    with pytest.raises(ProviderError) as failure:
        router.resolve(world.context(), "chat")

    assert failure.value.status_code == 502
    assert failure.value.message == NO_KEY
    assert failure.value.details == {}
    assert world.acquire_calls == [world.provider.id]
    assert world.count(KEY_POOL_SELECT) == 1
    assert world.count(PROVIDER_SELECT) == 2
    text = json.dumps(failure.value.to_response_content())
    assert str(world.provider.id) not in text
    _leak_free(text, world)


# ---------------------------------------------------------------------------
# Case C: explicit test stub reaches acquisition and real provider construction
# ---------------------------------------------------------------------------


def test_explicit_test_stub_reaches_acquire_once(monkeypatch, close_world):
    world = _build_world(monkeypatch)
    close_world.append(world)
    router = ModelRouter(world.session, allow_test_stub=True)

    route = router.resolve(world.context(), "chat")

    assert world.acquire_calls == [world.provider.id]
    assert isinstance(route.provider, StubProvider)
    assert route.provider.api_key == STUB_PLACEHOLDER
    assert route.provider_slug == world.provider.slug
    assert route.model_name == "s1-test-model"
    assert route.provider.chat(messages=[], model="s1-test-model").content == ""


# ---------------------------------------------------------------------------
# Authorization failures stop before provider and key reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["unbound", "override", "foreign_owner", "wrong_use_case"])
def test_unauthorized_route_stops_before_provider_and_key(monkeypatch, close_world, case):
    world = _build_world(monkeypatch, binding=case != "unbound")
    close_world.append(world)
    router = ModelRouter(world.session, allow_test_stub=True)
    override = None
    expected = PermissionDeniedError if case != "unbound" else NotFoundError

    if case == "override":
        other = ModelProfile(
            id=uuid4(),
            tenant_id=world.tenant_id,
            name="other",
            use_case="chat",
            provider_id=world.provider.id,
            model_name="other",
            is_active=True,
        )
        world.session.add(other)
        world.session.commit()
        override = other.id
    elif case == "foreign_owner":
        world.profile.tenant_id = uuid4()
        world.session.commit()
    elif case == "wrong_use_case":
        world.profile.use_case = "embed"
        world.session.commit()

    world.statements.clear()
    with pytest.raises(expected):
        router.resolve(world.context(), "chat", profile_override=override)

    assert world.acquire_calls == []
    assert world.count(PROVIDER_SELECT) == 0
    assert world.count(KEY_POOL_SELECT) == 0


# ---------------------------------------------------------------------------
# Existing API error mapping through a standard local ASGI application
# ---------------------------------------------------------------------------


def _probe_app(session: Session, allow_test_stub: bool = False) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def identity(request, call_next):
        authorization = request.headers.get("Authorization")
        if authorization:
            claims = decode_bearer_token(authorization)
            request.state.auth_claims = claims
            request.state.tenant_ctx = TenantContext(
                tenant_id=UUID(claims.tenant_id),
                tenant_slug=claims.tenant_slug or "acme",
                actor_id=claims.sub,
                actor_type=claims.actor_type,
                roles=claims.roles,
                trace_id="s1-1-api",
            )
        return await call_next(request)

    def _session():
        yield session

    @app.post("/chat/s1-probe")
    def probe(
        _claims: AuthCtx,
        tenant_ctx: TenantCtx,
        db: DBSession,
        profile_override: str | None = None,
    ):
        router = ModelRouter(db, allow_test_stub=allow_test_stub)
        override = UUID(profile_override) if profile_override else None
        route = router.resolve(tenant_ctx, "chat", profile_override=override)
        return {"provider": route.provider_slug}

    app.dependency_overrides[get_db_session] = _session
    return app


def _token(tenant_id: UUID) -> dict[str, str]:
    claims = {
        "sub": "u_1",
        "tenant_id": str(tenant_id),
        "tenant_slug": "acme",
        "actor_type": "end_user",
        "roles": ["end_user"],
    }
    return {"Authorization": f"Bearer {encode_dev_token(claims)}"}


def test_api_missing_identity_is_401(monkeypatch, close_world):
    world = _build_world(monkeypatch)
    close_world.append(world)
    client = TestClient(_probe_app(world.session))

    response = client.post("/chat/s1-probe")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert world.acquire_calls == []


def test_api_unauthorized_override_is_403_without_provider_reads(monkeypatch, close_world):
    world = _build_world(monkeypatch)
    close_world.append(world)
    other = ModelProfile(
        id=uuid4(),
        tenant_id=world.tenant_id,
        name="other",
        use_case="chat",
        provider_id=world.provider.id,
        model_name="other",
        is_active=True,
    )
    world.session.add(other)
    world.session.commit()
    client = TestClient(_probe_app(world.session, allow_test_stub=True))

    world.statements.clear()
    response = client.post(
        f"/chat/s1-probe?profile_override={other.id}", headers=_token(world.tenant_id)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert str(other.id) not in response.text
    assert world.acquire_calls == []
    assert world.count(PROVIDER_SELECT) == 0
    assert world.count(KEY_POOL_SELECT) == 0


def test_api_unavailable_authorized_provider_is_generic_502(monkeypatch, close_world):
    world = _build_world(monkeypatch, provider_active=False)
    close_world.append(world)
    client = TestClient(_probe_app(world.session))

    response = client.post("/chat/s1-probe", headers=_token(world.tenant_id))

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "PROVIDER_ERROR",
        "message": UNAVAILABLE,
        "details": {},
    }
    _leak_free(response.text, world)
    assert world.acquire_calls == []
