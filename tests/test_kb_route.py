from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import ConflictError, NotFoundError
from ragos.storage.models.app import ChunkManifest, DocumentRecord, KnowledgeBase, Task
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import kb as kb_route

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"
OTHER_KB_ID = "99999999-9999-9999-9999-999999999999"
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


class _Calls:
    """Shared scratch pad captured across fake repos for a single test."""

    def __init__(self) -> None:
        self.kb_created: list[dict[str, Any]] = []
        self.docs_created: list[dict[str, Any]] = []
        self.manifests_created: list[dict[str, Any]] = []
        self.tasks_enqueued: list[dict[str, Any]] = []
        self.require_kb_calls: list[UUID] = []
        self.require_doc_calls: list[tuple[UUID, UUID]] = []


class _FakeKBRepo:
    calls: _Calls
    conflict_slugs: set[str] = set()

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def list(self, *, limit: int, offset: int) -> list[KnowledgeBase]:
        return [
            KnowledgeBase(
                id=UUID("11111111-1111-1111-1111-111111111111"),
                tenant_id=UUID(TENANT_ID),
                slug="general",
                name="General",
                description=None,
            )
        ]

    def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None = None,
    ) -> KnowledgeBase:
        if slug in _FakeKBRepo.conflict_slugs:
            raise ConflictError("Knowledge base slug already exists", slug=slug)
        kb = KnowledgeBase(
            id=uuid4(),
            tenant_id=UUID(TENANT_ID),
            slug=slug,
            name=name,
            description=description,
        )
        _FakeKBRepo.calls.kb_created.append({"slug": slug, "name": name, "id": str(kb.id)})
        return kb

    def require_by_id(self, kb_id: UUID) -> KnowledgeBase:
        _FakeKBRepo.calls.require_kb_calls.append(kb_id)
        if str(kb_id) == MISSING_ID:
            raise NotFoundError(
                "KnowledgeBase not found", resource="KnowledgeBase", id=str(kb_id)
            )
        return KnowledgeBase(
            id=kb_id,
            tenant_id=UUID(TENANT_ID),
            slug="general",
            name="General",
            description=None,
        )


class _FakeDocRepo:
    calls: _Calls

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def list_for_kb(
        self,
        kb_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[DocumentRecord]:
        return [
            DocumentRecord(
                id=UUID("22222222-2222-2222-2222-222222222222"),
                tenant_id=UUID(TENANT_ID),
                kb_id=kb_id,
                title="Doc A",
            )
        ]

    def create(
        self,
        *,
        kb_id: UUID,
        title: str,
        external_id: str | None = None,
        source_uri: str | None = None,
    ) -> DocumentRecord:
        doc = DocumentRecord(
            id=uuid4(),
            tenant_id=UUID(TENANT_ID),
            kb_id=kb_id,
            title=title,
            external_id=external_id,
            source_uri=source_uri,
        )
        _FakeDocRepo.calls.docs_created.append(
            {"kb_id": str(kb_id), "title": title, "id": str(doc.id)}
        )
        return doc

    def require_in_kb(self, document_id: UUID, kb_id: UUID) -> DocumentRecord:
        _FakeDocRepo.calls.require_doc_calls.append((document_id, kb_id))
        if str(document_id) == MISSING_ID:
            raise NotFoundError(
                "DocumentRecord not found",
                resource="DocumentRecord",
                id=str(document_id),
            )
        return DocumentRecord(
            id=document_id,
            tenant_id=UUID(TENANT_ID),
            kb_id=kb_id,
            title="Doc A",
        )


class _FakeManifestRepo:
    calls: _Calls

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def create(self, *, kb_id: UUID, document_id: UUID) -> ChunkManifest:
        manifest = ChunkManifest(
            id=uuid4(),
            tenant_id=UUID(TENANT_ID),
            kb_id=kb_id,
            document_id=document_id,
            chunk_total=0,
            indexing_state="pending",
        )
        _FakeManifestRepo.calls.manifests_created.append(
            {
                "id": str(manifest.id),
                "kb_id": str(kb_id),
                "document_id": str(document_id),
            }
        )
        return manifest


class _FakeTaskRepo:
    calls: _Calls

    def __init__(self, session: Any, tenant_ctx: Any) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: Any = None,
    ) -> Task:
        task = Task(
            id=uuid4(),
            tenant_id=UUID(TENANT_ID),
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        _FakeTaskRepo.calls.tasks_enqueued.append(
            {
                "id": str(task.id),
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        return task


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    calls = _Calls()
    _FakeKBRepo.calls = calls
    _FakeKBRepo.conflict_slugs = set()
    _FakeDocRepo.calls = calls
    _FakeManifestRepo.calls = calls
    _FakeTaskRepo.calls = calls

    monkeypatch.setattr(kb_route, "KnowledgeBaseRepository", _FakeKBRepo)
    monkeypatch.setattr(kb_route, "DocumentRepository", _FakeDocRepo)
    monkeypatch.setattr(kb_route, "ChunkManifestRepository", _FakeManifestRepo)
    monkeypatch.setattr(kb_route, "TaskRepository", _FakeTaskRepo)

    fake_session = _FakeSession()

    def _override_session():
        yield fake_session

    app = create_app(testing=True)
    app.dependency_overrides[get_db_session] = _override_session
    client = TestClient(app)
    client.calls = calls  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_kb_route_rejects_unauthenticated(client: TestClient) -> None:
    resp = client.get("/tenant/kb")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_kb_route_rejects_non_tenant_admin(client: TestClient) -> None:
    resp = client.get("/tenant/kb", headers=_headers(["end_user"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Knowledge base CRUD
# ---------------------------------------------------------------------------


def test_kb_route_list_returns_items(client: TestClient) -> None:
    resp = client.get("/tenant/kb", headers=_headers(["tenant_admin"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["slug"] == "general"


def test_kb_route_create_kb(client: TestClient) -> None:
    resp = client.post(
        "/tenant/kb",
        headers=_headers(["tenant_admin"]),
        json={"slug": "product-docs", "name": "Product Docs"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "product-docs"
    assert body["name"] == "Product Docs"
    assert client.calls.kb_created[0]["slug"] == "product-docs"  # type: ignore[attr-defined]


def test_kb_route_create_kb_conflict(client: TestClient) -> None:
    _FakeKBRepo.conflict_slugs = {"dup"}
    resp = client.post(
        "/tenant/kb",
        headers=_headers(["tenant_admin"]),
        json={"slug": "dup", "name": "Dup"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_kb_route_get_missing(client: TestClient) -> None:
    resp = client.get(
        f"/tenant/kb/{MISSING_ID}",
        headers=_headers(["tenant_admin"]),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Document CRUD
# ---------------------------------------------------------------------------


def test_kb_route_create_document_validates_kb(client: TestClient) -> None:
    kb_id = "11111111-1111-1111-1111-111111111111"
    resp = client.post(
        f"/tenant/kb/{kb_id}/documents",
        headers=_headers(["tenant_admin"]),
        json={"title": "Doc 1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Doc 1"
    assert body["kb_id"] == kb_id
    # KB was validated before the doc was created.
    assert client.calls.require_kb_calls[0] == UUID(kb_id)  # type: ignore[attr-defined]


def test_kb_route_create_document_missing_kb(client: TestClient) -> None:
    resp = client.post(
        f"/tenant/kb/{MISSING_ID}/documents",
        headers=_headers(["tenant_admin"]),
        json={"title": "Doc 1"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Ingest trigger
# ---------------------------------------------------------------------------


def test_kb_route_ingest_creates_manifest_and_enqueues_task(client: TestClient) -> None:
    kb_id = "11111111-1111-1111-1111-111111111111"
    doc_id = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        f"/tenant/kb/{kb_id}/documents/{doc_id}/ingest",
        headers=_headers(["tenant_admin"]),
        json={
            "tier": "stable",
            "chunks": [
                {"body": "hello world", "title": "Intro"},
                {"body": "second chunk", "layer": "L2"},
            ],
        },
    )
    assert resp.status_code == 202
    body = resp.json()

    calls: _Calls = client.calls  # type: ignore[attr-defined]
    assert len(calls.manifests_created) == 1
    assert len(calls.tasks_enqueued) == 1

    manifest = calls.manifests_created[0]
    task = calls.tasks_enqueued[0]

    assert manifest["kb_id"] == kb_id
    assert manifest["document_id"] == doc_id

    assert task["kind"] == "ingest_document"
    assert task["payload"]["manifest_id"] == manifest["id"]
    assert task["payload"]["tier"] == "stable"
    assert len(task["payload"]["chunks"]) == 2
    # Every chunk is stamped with the URL-provided kb + doc.
    for chunk in task["payload"]["chunks"]:
        assert chunk["kb_id"] == kb_id
        assert chunk["document_id"] == doc_id
        assert chunk["body"]
    # Default idempotency_key derives from manifest id.
    assert task["idempotency_key"] == f"ingest-{manifest['id']}"

    assert body["manifest"]["id"] == manifest["id"]
    assert body["task_id"] == task["id"]


def test_kb_route_ingest_respects_custom_idempotency_key(client: TestClient) -> None:
    kb_id = "11111111-1111-1111-1111-111111111111"
    doc_id = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        f"/tenant/kb/{kb_id}/documents/{doc_id}/ingest",
        headers=_headers(["tenant_admin"]),
        json={
            "tier": "hot",
            "idempotency_key": "batch-2026-04-11-xyz",
            "chunks": [{"body": "only one"}],
        },
    )
    assert resp.status_code == 202
    calls: _Calls = client.calls  # type: ignore[attr-defined]
    assert calls.tasks_enqueued[0]["idempotency_key"] == "batch-2026-04-11-xyz"
    assert calls.tasks_enqueued[0]["payload"]["tier"] == "hot"


def test_kb_route_ingest_rejects_missing_document(client: TestClient) -> None:
    kb_id = "11111111-1111-1111-1111-111111111111"
    resp = client.post(
        f"/tenant/kb/{kb_id}/documents/{MISSING_ID}/ingest",
        headers=_headers(["tenant_admin"]),
        json={"chunks": [{"body": "x"}]},
    )
    assert resp.status_code == 404
    calls: _Calls = client.calls  # type: ignore[attr-defined]
    assert calls.tasks_enqueued == []
    assert calls.manifests_created == []


def test_kb_route_ingest_requires_at_least_one_chunk(client: TestClient) -> None:
    kb_id = "11111111-1111-1111-1111-111111111111"
    doc_id = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        f"/tenant/kb/{kb_id}/documents/{doc_id}/ingest",
        headers=_headers(["tenant_admin"]),
        json={"chunks": []},
    )
    assert resp.status_code == 422
