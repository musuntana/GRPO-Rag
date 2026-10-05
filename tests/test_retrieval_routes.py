"""Retrieval and retriever-profile routes.

The claims under test: a caller cannot widen its own scope through the body, a
profile configuration that the read path could not honour is refused before it is
stored, and a retrieval response reports the capabilities it actually had.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import Citation, CitationStatus
from ragos.retrieval.pipeline import RAGResult, RetrievalExplain
from ragos.storage.models.app import RetrieverProfileVersion
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import retrieval as retrieval_route
from ragos_api.routes import retriever_profiles as profiles_route

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"
OTHER_TENANT_ID = "11111111-2222-3333-4444-555555555555"
PROFILE_ID = "33333333-3333-3333-3333-333333333333"
MISSING_ID = "00000000-0000-0000-0000-000000000000"


def _token(roles: list[str], tenant_id: str = TENANT_ID) -> str:
    return encode_dev_token(
        {
            "sub": "u_admin",
            "tenant_id": tenant_id,
            "tenant_slug": "acme",
            "actor_type": "tenant_admin" if "tenant_admin" in roles else "end_user",
            "roles": roles,
        }
    )


def _headers(roles: list[str], tenant_id: str = TENANT_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(roles, tenant_id)}", "X-Tenant-Id": tenant_id}


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True


class _ProfileCalls:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.scope_keys: list[str] = []
        self.latest: dict[str, Any] = {}


class _FakeProfileRepo:
    calls: _ProfileCalls

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def list_profiles(self, *, scope_key: str, limit: int, offset: int):
        _FakeProfileRepo.calls.scope_keys.append(scope_key)
        return list(_FakeProfileRepo.calls.latest.values())

    def list_versions(self, *, scope_key: str, profile_id: UUID):
        _FakeProfileRepo.calls.scope_keys.append(scope_key)
        return [_row(profile_id, version=2), _row(profile_id, version=1)]

    def require_latest_version(self, *, scope_key: str, profile_id: UUID):
        _FakeProfileRepo.calls.scope_keys.append(scope_key)
        if str(profile_id) == MISSING_ID:
            raise NotFoundError("Retriever profile not found", profile_id=str(profile_id))
        return _row(profile_id)

    def require_version(self, *, scope_key: str, profile_id: UUID, version: int):
        if version == 99:
            raise NotFoundError(
                "Retriever profile version not found", profile_id=str(profile_id), version=version
            )
        return _row(profile_id, version=version)

    def create_version(self, *, scope_key: str, profile_id: UUID, **fields):
        _FakeProfileRepo.calls.scope_keys.append(scope_key)
        _FakeProfileRepo.calls.created.append({"profile_id": str(profile_id), **fields})
        return _row(profile_id, **fields)


DEFAULT_PROFILE_ID = UUID(PROFILE_ID)


def _row(profile_id: UUID = DEFAULT_PROFILE_ID, **overrides) -> RetrieverProfileVersion:
    fields = {
        "id": uuid4(),
        "tenant_id": UUID(TENANT_ID),
        "profile_id": profile_id,
        "version": 1,
        "name": "default",
        "query_strategy": "direct",
        "recall_channels": ["lexical"],
        "join_mode": "reciprocal_rank_fusion",
        "channel_budgets": {"lexical": 1.0},
        "fusion_config": {},
        "crag_thresholds": {},
        "crag_enabled": True,
        "rerank_model_profile": None,
        "citation_verify_semantic": False,
        "top_k_recall": 50,
        "top_k_rerank": 20,
        "top_k_generate": 6,
    }
    fields.update(overrides)
    return RetrieverProfileVersion(**fields)


class _FakeRetrievalService:
    """Stands in for the service so the route is tested for shape and scope."""

    calls: list[dict[str, Any]] = []
    outcome: Any = None
    error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def query(self, **kwargs: Any):
        _FakeRetrievalService.calls.append({**kwargs, "tenant_ctx": self.kwargs.get("tenant_ctx")})
        if _FakeRetrievalService.error is not None:
            raise _FakeRetrievalService.error
        return _FakeRetrievalService.outcome


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    calls = _ProfileCalls()
    calls.latest = {PROFILE_ID: _row()}
    _FakeProfileRepo.calls = calls
    _FakeRetrievalService.calls = []
    _FakeRetrievalService.error = None
    _FakeRetrievalService.outcome = _outcome()

    monkeypatch.setattr(profiles_route, "RetrieverProfileRepository", _FakeProfileRepo)
    monkeypatch.setattr(retrieval_route, "RetrievalService", _FakeRetrievalService)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    client = TestClient(app)
    client.calls = calls  # type: ignore[attr-defined]
    client.session = fake_session  # type: ignore[attr-defined]
    return client


def _outcome(warnings: list[str] | None = None):
    from ragos.retrieval.types import RetrieverProfile, ScoredChunk

    chunk = ScoredChunk(
        chunk_id=uuid4(),
        doc_id=uuid4(),
        kb_id=uuid4(),
        text="evidence",
        score=0.9,
        channel="lexical",
        page_no=3,
    )
    explain = RetrievalExplain(
        rewritten_query="rewritten",
        per_channel_counts={"lexical": 1},
        channel_budgets={"lexical": 1.0},
        refined_query="refined",
        recall_queries=["refined"],
        query_strategy="direct",
        citation_supported=1,
        citation_generated=1,
    )
    profile = RetrieverProfile(
        id=uuid4(),
        tenant_id=UUID(TENANT_ID),
        recall_channels=["lexical"],
        channel_budgets={"lexical": 1.0},
    )
    result = RAGResult(
        answer="answer [1].",
        citations=[
            Citation(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                page_no=3,
                start_char_idx=1,
                end_char_idx=40,
                claim="answer [1].",
                text="evidence",
                status=CitationStatus.VERIFIED,
                marker_index=1,
            )
        ],
        retrieval_explain=explain,
        evidence=(chunk,),
        gate=None,
        abstained=False,
        token_usage={},
    )
    from ragos.runtime.retrieval import RetrievalOutcome

    return RetrievalOutcome(
        result=result, profile=profile, profile_name="default", warnings=warnings or []
    )


# -- authorization ----------------------------------------------------------


def test_retrieval_rejects_unauthenticated(client: TestClient) -> None:
    assert client.post("/tenant/retrieval/query", json={"query": "q"}).status_code == 401


def test_retrieval_rejects_a_non_tenant_admin(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["end_user"]),
        json={"query": "q", "retriever_profile_id": PROFILE_ID},
    )
    assert resp.status_code == 403


def test_profile_routes_reject_a_non_tenant_admin(client: TestClient) -> None:
    resp = client.get("/tenant/retriever-profiles", headers=_headers(["end_user"]))

    assert resp.status_code == 403


# -- retriever profiles -----------------------------------------------------


def test_list_profiles_returns_latest_versions_with_the_scope_key(client: TestClient) -> None:
    resp = client.get("/tenant/retriever-profiles", headers=_headers(["tenant_admin"]))

    assert resp.status_code == 200
    assert resp.json()["items"]
    calls: _ProfileCalls = client.calls  # type: ignore[attr-defined]
    assert calls.scope_keys[0].startswith(TENANT_ID)


def test_create_profile_validates_before_storing(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retriever-profiles",
        headers=_headers(["tenant_admin"]),
        json={"name": "chat", "recall_channels": ["lexical"], "channel_budgets": {"lexical": 1.0}},
    )

    assert resp.status_code == 201
    assert resp.json()["name"] == "chat"
    assert client.session.committed is True  # type: ignore[attr-defined]


def test_create_profile_rejects_budgets_the_read_path_cannot_honour(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retriever-profiles",
        headers=_headers(["tenant_admin"]),
        json={
            "name": "broken",
            "recall_channels": ["lexical", "dense"],
            "channel_budgets": {"lexical": 0.7, "dense": 0.7},
        },
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing was committed: an invalid configuration never reaches storage.
    assert client.session.committed is False  # type: ignore[attr-defined]


def test_create_profile_defaults_budgets_to_the_documented_weights(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retriever-profiles",
        headers=_headers(["tenant_admin"]),
        json={"name": "defaulted", "recall_channels": ["lexical", "dense"]},
    )

    assert resp.status_code == 201
    calls: _ProfileCalls = client.calls  # type: ignore[attr-defined]
    assert calls.created[0]["channel_budgets"] == {"lexical": 0.2, "dense": 0.45}


def test_a_new_version_inherits_the_fields_the_request_omits(client: TestClient) -> None:
    resp = client.post(
        f"/tenant/retriever-profiles/{PROFILE_ID}/versions",
        headers=_headers(["tenant_admin"]),
        json={"top_k_generate": 4},
    )

    assert resp.status_code == 201
    calls: _ProfileCalls = client.calls  # type: ignore[attr-defined]
    created = calls.created[0]
    assert created["top_k_generate"] == 4
    assert created["recall_channels"] == ["lexical"]
    assert created["query_strategy"] == "direct"


def test_get_a_missing_profile_is_404(client: TestClient) -> None:
    resp = client.get(
        f"/tenant/retriever-profiles/{MISSING_ID}", headers=_headers(["tenant_admin"])
    )

    assert resp.status_code == 404


def test_listing_versions_of_a_missing_profile_is_404_free_but_empty(client: TestClient) -> None:
    resp = client.get(
        f"/tenant/retriever-profiles/{PROFILE_ID}/versions", headers=_headers(["tenant_admin"])
    )

    assert resp.status_code == 200
    assert [item["version"] for item in resp.json()["items"]] == [2, 1]


# -- caller-supplied scope --------------------------------------------------


def test_a_body_supplied_tenant_id_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={
            "query": "q",
            "retriever_profile_id": PROFILE_ID,
            "tenant_id": OTHER_TENANT_ID,
        },
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _FakeRetrievalService.calls == [], "the request must not reach the pipeline"


def test_a_body_supplied_scope_key_is_refused_even_when_it_matches(client: TestClient) -> None:
    """It is server-side state by construction; a matching value is still an attempt."""
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={
            "query": "q",
            "retriever_profile_id": PROFILE_ID,
            "scope_key": f"{TENANT_ID}::u_admin",
        },
    )

    assert resp.status_code == 403


def test_a_body_supplied_kb_id_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={
            "query": "q",
            "retriever_profile_id": PROFILE_ID,
            "kb_id": "77777777-7777-7777-7777-777777777777",
        },
    )

    assert resp.status_code == 403


def test_a_matching_scope_field_is_discarded_rather_than_honoured(client: TestClient) -> None:
    """Consistent with the derived scope, so the request proceeds — under the *server's*
    scope. Honouring the body value would mean two sources of identity, one of them
    supplied by the caller."""
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "q", "retriever_profile_id": PROFILE_ID, "tenant_id": TENANT_ID},
    )

    assert resp.status_code == 200
    tenant_ctx = _FakeRetrievalService.calls[0]["tenant_ctx"]
    assert str(tenant_ctx.tenant_id) == TENANT_ID
    assert tenant_ctx.scope_key == f"{TENANT_ID}::u_admin"


def test_a_profile_write_with_a_forged_tenant_id_is_refused(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retriever-profiles",
        headers=_headers(["tenant_admin"]),
        json={"name": "x", "tenant_id": OTHER_TENANT_ID},
    )

    assert resp.status_code == 403


# -- retrieval query --------------------------------------------------------


def test_retrieval_returns_the_answer_citations_and_explain(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "what is the policy?", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "answer [1]."
    assert body["abstained"] is False
    assert body["citations"][0]["status"] == "verified"
    assert body["explain"]["refined_query"] == "refined"
    assert body["explain"]["per_channel_counts"] == {"lexical": 1}
    assert body["evidence"][0]["page_no"] == 3


def test_retrieval_reports_a_capability_it_did_not_have(client: TestClient) -> None:
    _FakeRetrievalService.outcome = _outcome(warnings=["recall channel 'dense' is not wired"])

    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "q", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 200
    assert resp.json()["warnings"] == ["recall channel 'dense' is not wired"]


def test_retrieval_passes_the_pinned_version_through(client: TestClient) -> None:
    client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={
            "query": "q",
            "retriever_profile_id": PROFILE_ID,
            "retriever_profile_version": 3,
            "conversation_history": ["earlier question"],
        },
    )

    call = _FakeRetrievalService.calls[0]
    assert call["profile_version"] == 3
    assert call["conversation_history"] == ["earlier question"]


def test_retrieval_requires_a_profile_id(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query", headers=_headers(["tenant_admin"]), json={"query": "q"}
    )

    assert resp.status_code == 422


def test_a_missing_profile_surfaces_as_not_found(client: TestClient) -> None:
    _FakeRetrievalService.error = NotFoundError("Retriever profile not found")

    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "q", "retriever_profile_id": MISSING_ID},
    )

    assert resp.status_code == 404


def test_semantic_verification_without_an_entailment_callable_is_refused(
    client: TestClient,
) -> None:
    _FakeRetrievalService.error = ValidationError(
        "profile requests semantic citation verification but no entailment callable is wired"
    )

    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "q", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 422


def test_a_cross_tenant_profile_id_is_not_found_rather_than_found(client: TestClient) -> None:
    """The repository filters by tenant, so another tenant's profile is simply absent."""
    _FakeRetrievalService.error = NotFoundError("Retriever profile not found")

    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"], tenant_id=OTHER_TENANT_ID),
        json={"query": "q", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 404


def test_the_response_carries_the_request_id(client: TestClient) -> None:
    resp = client.post(
        "/tenant/retrieval/query",
        headers=_headers(["tenant_admin"]),
        json={"query": "q", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.headers.get("X-Request-Id")


del asyncio, ConflictError, PermissionDeniedError, TenantContext
