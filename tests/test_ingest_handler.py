from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from ragos.core.tenant import ActorType, TenantContext
from ragos.runtime import tasks as tasks_module
from ragos.runtime.tasks import decode_chunk_upsert, make_ingest_document_handler
from ragos.storage.models.app import Task
from ragos.storage.repositories.search import SearchTier


def _tenant_ctx(tenant_id: UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid4(),
        tenant_slug="acme",
        actor_id="worker",
        actor_type=ActorType.SERVICE,
        roles=["service"],
        trace_id="trace-ingest",
    )


def _task(payload: dict[str, Any]) -> Task:
    return Task(
        id=uuid4(),
        tenant_id=uuid4(),
        kind="ingest_document",
        payload=payload,
        idempotency_key=f"idem-{uuid4()}",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_decode_chunk_upsert_fixes_tenant_id() -> None:
    ctx = _tenant_ctx()
    raw = {
        "id": str(uuid4()),
        "body": "hello",
        # deliberately pass a *different* tenant_id to prove it gets overwritten
        "tenant_id": str(uuid4()),
        "kb_id": str(uuid4()),
        "document_id": str(uuid4()),
        "layer": "L2",
        "lang": "zh",
        "title": "t",
        "headings_text": None,
        "tags_text": "tag",
        "metadata_json": {"k": "v"},
    }

    chunk = decode_chunk_upsert(raw, ctx)

    assert chunk.tenant_id == ctx.tenant_id
    assert chunk.body == "hello"
    assert chunk.layer == "L2"
    assert chunk.metadata_json == {"k": "v"}
    assert chunk.updated_at is not None


def test_ingest_document_handler_invokes_service_without_opening_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _tenant_ctx()
    manifest_id = uuid4()
    payload = {
        "manifest_id": str(manifest_id),
        "tier": "stable",
        "chunks": [
            {"id": str(uuid4()), "body": "alpha"},
            {"id": str(uuid4()), "body": "beta"},
        ],
    }
    task = _task(payload)

    captured: dict[str, Any] = {}

    class _FakeService:
        def __init__(self, session: Any, tenant_ctx: TenantContext, **kwargs: Any) -> None:
            captured["service_session"] = session
            captured["service_tenant_ctx"] = tenant_ctx
            captured["service_vector_writer"] = kwargs.get("vector_writer")

        def index_chunks(self, *, manifest_id, tier, chunks):
            captured["manifest_id"] = manifest_id
            captured["tier"] = tier
            captured["chunks"] = chunks

            class _Result:
                def __init__(self) -> None:
                    self.manifest_id = manifest_id
                    self.chunk_total = len(chunks)
                    self.indexing_state = "indexed"

            return _Result()

    monkeypatch.setattr(tasks_module, "IngestionService", _FakeService)

    sentinel_session = object()
    sentinel_writer = object()
    handler = make_ingest_document_handler(vector_writer=sentinel_writer)  # type: ignore[arg-type]

    handler(task=task, tenant_ctx=ctx, session=sentinel_session)  # type: ignore[arg-type]

    assert captured["service_session"] is sentinel_session
    assert captured["service_tenant_ctx"] is ctx
    assert captured["service_vector_writer"] is sentinel_writer
    assert captured["manifest_id"] == manifest_id
    assert captured["tier"] == SearchTier.STABLE
    assert len(captured["chunks"]) == 2
    assert all(c.tenant_id == ctx.tenant_id for c in captured["chunks"])


def test_ingest_document_handler_raises_on_missing_manifest_id() -> None:
    handler = make_ingest_document_handler()
    task = _task({"tier": "stable", "chunks": []})

    with pytest.raises(ValueError, match="ingest_document payload invalid"):
        handler(task=task, tenant_ctx=_tenant_ctx(), session=object())  # type: ignore[arg-type]
