from __future__ import annotations

import json
import logging
import traceback
from typing import Annotated, Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import Body, FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ValidationError
from ragos.storage.models.app import KeyPool
from ragos.storage.repositories import model_gateway as model_gateway_repo
from ragos.storage.repositories.model_gateway import KeyPoolRepository
from ragos_api.dependencies import get_db_session
from ragos_api.errors import register_error_handlers
from ragos_api.main import create_app

SENTINEL = "sk-S13-SENTINEL-7f3a9c2e-do-not-persist"
ALNUM_SENTINEL = "S13AlnumSecretSentinel9x"
KEY_POOL_OPENAPI_PATH = "/platform/model-center/providers/{provider_id}/key-pools"
ENCRYPTED_LOOKING = [
    SENTINEL,
    "enc:v1:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 2,
    "fernet:gAAAAABh" + "a" * 80,
    "sk-" + "A" * 40,
]


class _ObservingSession:
    """Records every session method call; any call is an unexpected write or read."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def _record(*args: Any, **kwargs: Any) -> MagicMock:
            self.calls.append(name)
            return MagicMock()

        return _record


@pytest.fixture
def spy_key_pool(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    constructed: list[int] = []
    real = KeyPool

    def _spy(**kwargs: Any) -> KeyPool:
        constructed.append(1)
        return real(**kwargs)

    monkeypatch.setattr(model_gateway_repo, "KeyPool", _spy)
    return constructed


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


@pytest.fixture
def observed() -> _ObservingSession:
    return _ObservingSession()


@pytest.fixture
def client(observed: _ObservingSession) -> TestClient:
    app = create_app(testing=True)

    def _override_session():
        yield observed

    app.dependency_overrides[get_db_session] = _override_session
    return TestClient(app, raise_server_exceptions=False)


def _key_pool_path() -> str:
    return f"/platform/model-center/providers/{uuid4()}/key-pools"


def _valid_key_body(api_key: str = SENTINEL) -> dict[str, Any]:
    return {"label": "primary", "api_key": api_key, "weight": 2}


# -- Repository.create direct call --


@pytest.mark.parametrize("api_key", ENCRYPTED_LOOKING)
def test_repository_create_rejects_raw_key_before_orm_or_session_io(
    api_key: str,
    observed: _ObservingSession,
    spy_key_pool: list[int],
) -> None:
    repo = KeyPoolRepository(observed)  # type: ignore[arg-type]

    with pytest.raises(ValidationError) as excinfo:
        repo.create(provider_id=uuid4(), label="primary", api_key=api_key, weight=1)

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "VALIDATION_ERROR"
    assert observed.calls == []
    assert spy_key_pool == []


def test_repository_create_rejection_payload_never_contains_secret(
    observed: _ObservingSession,
) -> None:
    repo = KeyPoolRepository(observed)  # type: ignore[arg-type]

    with pytest.raises(ValidationError) as excinfo:
        repo.create(provider_id=uuid4(), label="primary", api_key=SENTINEL, weight=1)

    exc = excinfo.value
    serialized = json.dumps(exc.to_response_content())
    rendered = "".join(traceback.format_exception(exc))
    assert SENTINEL not in str(exc)
    assert SENTINEL not in repr(exc)
    assert SENTINEL not in serialized
    assert SENTINEL not in rendered
    assert SENTINEL not in json.dumps(exc.details, default=str)
    assert exc.__context__ is None
    assert exc.__cause__ is None


def test_repository_create_has_no_trusted_bypass_parameter() -> None:
    import inspect

    params = inspect.signature(KeyPoolRepository.create).parameters
    forbidden = {"encrypted", "trusted", "ciphertext", "api_key_encrypted", "skip_validation"}
    assert forbidden.isdisjoint(params)


# -- API: authorized, valid-format raw credential --


def test_create_key_pool_authorized_valid_key_is_rejected_without_session_writes(
    client: TestClient,
    observed: _ObservingSession,
    spy_key_pool: list[int],
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json=_valid_key_body(),
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert set(body["error"]) == {"code", "message", "details"}
    assert SENTINEL not in resp.text
    assert observed.calls == []
    assert spy_key_pool == []


def test_create_key_pool_unauthenticated_rejected_before_write(
    client: TestClient,
    observed: _ObservingSession,
) -> None:
    resp = client.post(_key_pool_path(), json=_valid_key_body())

    assert resp.status_code == 401
    assert SENTINEL not in resp.text
    assert observed.calls == []


def test_create_key_pool_tenant_admin_forbidden_before_write(
    client: TestClient,
    observed: _ObservingSession,
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["tenant_admin"]),
        json=_valid_key_body(),
    )

    assert resp.status_code == 403
    assert SENTINEL not in resp.text
    assert observed.calls == []


# -- API: RequestValidationError sanitization --


def _assert_sanitized_validation(resp: Any, *, field: str, code: str) -> dict[str, Any]:
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert {"field": field, "code": code} in body["error"]["details"]["errors"]
    assert SENTINEL not in resp.text
    assert "input" not in resp.text
    assert "ctx" not in resp.text
    return body


def test_missing_api_key_returns_sanitized_422(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json={"label": "primary", "weight": 1, "note": SENTINEL},
    )

    _assert_sanitized_validation(resp, field="body.api_key", code="REQUIRED")
    assert observed.calls == []


def test_overlong_api_key_returns_sanitized_422(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json={"label": "primary", "api_key": SENTINEL + "x" * 600},
    )

    _assert_sanitized_validation(resp, field="body.api_key", code="TOO_LONG")
    assert observed.calls == []


def test_empty_api_key_returns_sanitized_422(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json={"label": "primary", "api_key": ""},
    )

    _assert_sanitized_validation(resp, field="body.api_key", code="TOO_SHORT")
    assert observed.calls == []


def test_wrong_type_api_key_returns_sanitized_422(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json={"label": "primary", "api_key": {"value": SENTINEL}},
    )

    _assert_sanitized_validation(resp, field="body.api_key", code="INVALID_TYPE")
    assert observed.calls == []


def test_multiple_invalid_fields_are_all_reported_without_values(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json={"label": "", "api_key": SENTINEL, "weight": "not-a-number"},
    )

    body = resp.json()
    assert resp.status_code == 422
    fields = {(e["field"], e["code"]) for e in body["error"]["details"]["errors"]}
    assert ("body.label", "TOO_SHORT") in fields
    assert ("body.weight", "INVALID_TYPE") in fields
    assert SENTINEL not in resp.text
    assert observed.calls == []


def test_malformed_json_returns_sanitized_422(
    client: TestClient, observed: _ObservingSession
) -> None:
    resp = client.post(
        _key_pool_path(),
        headers={**_headers(["platform_admin"]), "Content-Type": "application/json"},
        content=('{"label": "primary", "api_key": "' + SENTINEL + '", ').encode(),
    )

    _assert_sanitized_validation(resp, field="body", code="MALFORMED_JSON")
    assert observed.calls == []


def test_sentinel_in_path_parameter_is_not_echoed(client: TestClient) -> None:
    resp = client.get(
        f"/platform/model-center/providers/{SENTINEL}/key-pools",
        headers=_headers(["platform_admin"]),
    )

    _assert_sanitized_validation(resp, field="path.provider_id", code="INVALID_TYPE")


def test_sentinel_in_query_parameter_is_not_echoed(client: TestClient) -> None:
    resp = client.get(
        "/platform/model-center/providers",
        params={"limit": SENTINEL},
        headers=_headers(["platform_admin"]),
    )

    _assert_sanitized_validation(resp, field="query.limit", code="INVALID_TYPE")


def test_sentinel_in_profile_field_is_not_echoed(client: TestClient) -> None:
    resp = client.post(
        "/platform/model-center/profiles",
        headers=_headers(["platform_admin"]),
        json={
            "tenant_id": SENTINEL,
            "name": "gpt4",
            "use_case": "chat",
            "provider_id": str(uuid4()),
            "model_name": "gpt-4",
            "parameters": {SENTINEL: "x"},
        },
    )

    _assert_sanitized_validation(resp, field="body.tenant_id", code="INVALID_TYPE")


def test_validation_response_keeps_request_id(client: TestClient) -> None:
    resp = client.post(
        _key_pool_path(),
        headers={**_headers(["platform_admin"]), "X-Request-Id": "req-s13-validation"},
        json={"label": "primary"},
    )

    assert resp.status_code == 422
    assert resp.headers["X-Request-Id"] == "req-s13-validation"


def test_validation_and_rejection_logs_do_not_contain_secret(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    malformed = client.post(
        _key_pool_path(),
        headers={**_headers(["platform_admin"]), "Content-Type": "application/json"},
        content=('{"api_key": "' + SENTINEL + '"').encode(),
    )
    rejected = client.post(
        _key_pool_path(),
        headers=_headers(["platform_admin"]),
        json=_valid_key_body(),
    )

    assert malformed.status_code == 422
    assert rejected.status_code == 422
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.args)


# -- Field location allowlist: only declared route names may appear in `field` --


class _ProbeBody(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=512)


def _probe_app(raise_errors: list[dict[str, Any]] | None = None) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/probe/dict")
    def dict_body(payload: Annotated[dict[str, int], Body()]) -> dict[str, Any]:
        return {}

    @app.post("/probe/{provider_id}/model")
    def model_body(
        provider_id: int,
        payload: _ProbeBody,
        limit: int = Query(default=1),
    ) -> dict[str, Any]:
        if raise_errors is not None:
            raise RequestValidationError(raise_errors)
        return {}

    return app


def test_dict_body_dynamic_key_is_never_returned_as_field() -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    resp = client.post("/probe/dict", json={ALNUM_SENTINEL: "not-an-int"})

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"] == [
        {"field": "body", "code": "INVALID_TYPE"}
    ]
    assert ALNUM_SENTINEL not in resp.text


def test_model_body_extra_dynamic_key_is_not_returned_as_field() -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    resp = client.post(
        "/probe/1/model",
        json={"label": "", "api_key": "k", ALNUM_SENTINEL: 1},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"] == [
        {"field": "body.label", "code": "TOO_SHORT"}
    ]
    assert ALNUM_SENTINEL not in resp.text


def test_path_parameter_dynamic_value_is_not_echoed() -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    resp = client.post(
        f"/probe/{ALNUM_SENTINEL}/model",
        json={"label": "x", "api_key": "k"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"] == [
        {"field": "path.provider_id", "code": "INVALID_TYPE"}
    ]
    assert ALNUM_SENTINEL not in resp.text


_RAISED_CASES = [
    (("body", ALNUM_SENTINEL), "body"),
    (("body", "label"), "body.label"),
    (("body", "label", ALNUM_SENTINEL), "body.label"),
    (("body", "label", "str"), "body.label"),
    (("body", "undeclared_extra"), "body"),
    (("body", 0), "body"),
    (("query", ALNUM_SENTINEL), "query"),
    (("query", "limit"), "query.limit"),
    (("path", "provider_id"), "path.provider_id"),
    (("header", ALNUM_SENTINEL), "header"),
    (("cookie", "label"), "cookie"),
    ((ALNUM_SENTINEL,), "request"),
    (("weird_source", "label"), "request"),
    ((["unhashable", ALNUM_SENTINEL], "label"), "request"),
    (({"key": ALNUM_SENTINEL},), "request"),
    ((), "request"),
]


@pytest.mark.parametrize(("loc", "expected_field"), _RAISED_CASES, ids=repr)
def test_raised_validation_error_fields_are_allowlisted(
    loc: tuple[Any, ...],
    expected_field: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    errors = [
        {
            "type": "int_parsing",
            "loc": loc,
            "msg": ALNUM_SENTINEL,
            "input": ALNUM_SENTINEL,
            "ctx": {"error": ALNUM_SENTINEL},
        }
    ]
    client = TestClient(_probe_app(errors), raise_server_exceptions=False)

    resp = client.post("/probe/1/model", json={"label": "x", "api_key": "k"})

    assert resp.status_code == 422
    body = resp.json()
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["details"]["errors"] == [
        {"field": expected_field, "code": "INVALID_TYPE"}
    ]
    assert ALNUM_SENTINEL not in resp.text
    assert ALNUM_SENTINEL not in caplog.text


def test_raised_validation_error_serialization_has_no_value_for_mixed_locations() -> None:
    errors = [
        {"type": "missing", "loc": ("body", "api_key"), "msg": ALNUM_SENTINEL,
         "input": ALNUM_SENTINEL, "ctx": {"x": ALNUM_SENTINEL}},
        {"type": "string_too_long", "loc": ("body", ALNUM_SENTINEL), "msg": ALNUM_SENTINEL,
         "input": ALNUM_SENTINEL, "ctx": {"x": ALNUM_SENTINEL}},
        {"type": "int_parsing", "loc": ("query", ALNUM_SENTINEL), "msg": ALNUM_SENTINEL,
         "input": ALNUM_SENTINEL, "ctx": {"x": ALNUM_SENTINEL}},
    ]
    client = TestClient(_probe_app(errors), raise_server_exceptions=False)

    resp = client.post("/probe/1/model", json={"label": "x", "api_key": "k"})

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"] == [
        {"field": "body.api_key", "code": "REQUIRED"},
        {"field": "body", "code": "TOO_LONG"},
        {"field": "query", "code": "INVALID_TYPE"},
    ]
    assert ALNUM_SENTINEL not in resp.text


# -- OpenAPI contract for the raw credential route --


def test_create_key_pool_openapi_does_not_advertise_success() -> None:
    spec = create_app(testing=True).openapi()
    responses = spec["paths"][KEY_POOL_OPENAPI_PATH]["post"]["responses"]

    assert "200" not in responses
    assert "201" not in responses
    assert "422" in responses
    description = responses["422"]["description"].lower()
    assert "unavailable" in description
    assert "not accepted" in description
