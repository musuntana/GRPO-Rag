"""Mock repository preparation probes with real artifacts; not PostgreSQL evidence."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime import index_preparation as preparation
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan, PlannedChunk
from ragos.storage.artifacts import ArtifactStore


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="actor", trace_id="trace")
    state = dict(in_tx=False, tasks={}, manifests={}, writes=0)

    class Artifacts(ArtifactStore):
        def read_bytes(self, **kwargs):
            assert not state["in_tx"]
            return super().read_bytes(**kwargs)

        def put_bytes(self, **kwargs):
            assert not state["in_tx"]
            state["writes"] += 1
            return super().put_bytes(**kwargs)

    store = Artifacts(tmp_path)
    source_ref, source_digest = store.put_bytes(
        tenant_ctx=ctx, kind="source", data=b"actual source"
    )
    version, rep, doc, kb, index = [uuid4() for _ in range(5)]
    frozen = FrozenRepresentation(
        schema_version=1,
        document_version_id=version,
        source_digest=source_digest,
        source_type="plain",
        parser_version="synthetic-parser",
        render_version="none",
        parent_unit="token_window",
        text="original",
    )
    ref, digest = store.put_bytes(tenant_ctx=ctx, kind="representation", data=frozen.frozen_bytes())
    plan = IndexPlan(
        schema_version=1,
        tenant_id=ctx.tenant_id,
        kb_id=kb,
        document_id=doc,
        document_version_id=version,
        representation_id=rep,
        index_id=index,
        tier="stable",
        source_ref=source_ref,
        source_digest=source_digest,
        representation_ref=ref,
        representation_digest=digest,
        chunker_version="synthetic",
        chunker_config_digest="a" * 64,
        embedding_profile="synthetic",
        embedding_version="synthetic-model",
        parent_unit="token_window",
        representation_char_count=8,
        expected_active_index_id=None,
        chunks=(
            PlannedChunk(
                id=uuid4(),
                parent_chunk_id=None,
                is_parent=True,
                body="original",
                start_char_idx=0,
                end_char_idx=8,
                element_type="text",
            ),
        ),
    )
    source = SimpleNamespace(
        source_ref=source_ref, source_digest=source_digest, media_type="text/plain"
    )
    representation = SimpleNamespace(
        representation_ref=ref,
        representation_digest=digest,
        char_count=8,
        parent_unit="token_window",
        page_count=None,
        parser_version=frozen.parser_version,
        render_version=frozen.render_version,
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        @contextmanager
        def begin(self):
            state["in_tx"] = True
            try:
                yield
            finally:
                state["in_tx"] = False

    class Versions:
        def __init__(self, *_args):
            pass

        def require_in_document(self, **_kwargs):
            return source

    class Representations:
        def __init__(self, *_args):
            pass

        def require_in_version(self, **_kwargs):
            return representation

    class Manifests:
        def __init__(self, *_args):
            pass

        def require_by_id(self, manifest_id):
            return state["manifests"][manifest_id]

        def create(self, **fields):
            assert state["in_tx"]
            manifest = SimpleNamespace(
                id=fields.pop("index_id"),
                indexing_state="pending",
                tenant_id=ctx.tenant_id,
                parent_unit=representation.parent_unit,
                page_count=representation.page_count,
                **fields,
            )
            state["manifests"][manifest.id] = manifest
            return manifest

    class Tasks:
        def __init__(self, *_args):
            pass

        def lock_preparation_key(self, _key):
            assert state["in_tx"]

        def get_by_idempotency_key(self, key):
            return state["tasks"].get(key)

        def enqueue(self, **fields):
            task = SimpleNamespace(id=uuid4(), **fields)
            state["tasks"][fields["idempotency_key"]] = task
            return task

    monkeypatch.setattr(preparation, "bind_session_tenant", lambda *_args: None)
    monkeypatch.setattr(preparation, "DocumentVersionRepository", Versions)
    monkeypatch.setattr(preparation, "DocumentRepresentationRepository", Representations)
    monkeypatch.setattr(preparation, "ChunkManifestRepository", Manifests)
    monkeypatch.setattr(preparation, "TaskRepository", Tasks)
    monkeypatch.setattr(preparation, "require_manifest_contract", lambda *_args: None)
    service = preparation.IndexPreparationService(Session, ctx, store)
    return SimpleNamespace(
        service=service,
        state=state,
        plan=plan,
        store=store,
        ctx=ctx,
        source=source,
        session_factory=Session,
    )


def test_prepare_persists_plan_and_manifest_only_task_without_artifact_io_under_lock(prepared):
    r = prepared
    result = r.service.prepare(plan=r.plan, idempotency_key="retry")
    assert result.manifest_id == r.plan.index_id
    assert result.indexing_state == "pending"
    task = next(iter(r.state["tasks"].values()))
    assert task.payload == {"manifest_id": str(result.manifest_id)}
    assert task.timeout_seconds == 300
    assert task.idempotency_key.startswith("prepare-index:")
    assert "retry" not in task.idempotency_key
    assert result.task_id == task.id


def test_retry_accepts_new_server_ids_but_returns_original_task_without_new_plan_write(prepared):
    r = prepared
    first = r.service.prepare(plan=r.plan, idempotency_key="retry")
    writes = r.state["writes"]
    equivalent = r.plan.model_copy(
        update={
            "index_id": uuid4(),
            "expected_active_index_id": uuid4(),
            "chunks": tuple(chunk.model_copy(update={"id": uuid4()}) for chunk in r.plan.chunks),
        }
    )
    assert r.service.prepare(plan=equivalent, idempotency_key="retry") == first
    assert len(r.state["tasks"]) == len(r.state["manifests"]) == 1
    assert r.state["writes"] == writes


@pytest.mark.parametrize("change", ["profile", "timeout"])
def test_same_key_with_changed_semantics_or_budget_is_refused(prepared, change):
    r = prepared
    r.service.prepare(plan=r.plan, idempotency_key="retry")
    candidate = (
        r.plan.model_copy(update={"embedding_version": "different-model"})
        if change == "profile"
        else r.plan
    )
    with pytest.raises(ConflictError, match="different plan or timeout"):
        r.service.prepare(
            plan=candidate,
            idempotency_key="retry",
            timeout_seconds=301 if change == "timeout" else 300,
        )
    assert len(r.state["tasks"]) == 1


def test_source_file_tampering_does_not_register_work(prepared):
    r = prepared
    (r.store.root / r.plan.source_ref).write_bytes(b"tampered")
    with pytest.raises(ConflictError, match="artifact digest"):
        r.service.prepare(plan=r.plan, idempotency_key="retry")
    assert not r.state["tasks"]
    assert not r.state["manifests"]


def test_mime_profile_mismatch_is_not_accepted_as_valid_representation(prepared):
    r = prepared
    r.source.media_type = "application/pdf"
    with pytest.raises(ValidationError, match="MIME"):
        r.service.prepare(plan=r.plan, idempotency_key="retry")
    assert not r.state["tasks"]


def test_retry_namespaces_actor_identity(prepared):
    r = prepared
    r.service.prepare(plan=r.plan, idempotency_key="retry")
    actor = r.ctx.model_copy(update={"actor_id": "another-actor"})
    other = preparation.IndexPreparationService(r.session_factory, actor, r.store)
    other.prepare(plan=r.plan.model_copy(update={"index_id": uuid4()}), idempotency_key="retry")
    assert len(r.state["tasks"]) == 2


def test_foreign_tenant_plan_is_denied_before_artifact_access(prepared):
    r = prepared
    with pytest.raises(PermissionDeniedError):
        r.service.prepare(
            plan=r.plan.model_copy(update={"tenant_id": uuid4()}), idempotency_key="retry"
        )
    assert not r.state["tasks"]
