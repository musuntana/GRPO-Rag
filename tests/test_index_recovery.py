"""Mock transaction/fault probes with real artifact bytes; not PostgreSQL evidence."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime import ingest
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan, PlannedChunk
from ragos.storage.artifacts import ArtifactStore
from ragos.storage.index_projection import projection_digest, projection_from_chunk


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="worker", trace_id="trace")
    store = ArtifactStore(tmp_path)
    source_ref, source_digest = store.put_bytes(tenant_ctx=ctx, kind="source", data=b"source bytes")
    version, representation_id, index_id, document, kb = [uuid4() for _ in range(5)]
    representation = FrozenRepresentation(
        schema_version=1,
        document_version_id=version,
        source_digest=source_digest,
        source_type="plain",
        parser_version="synthetic-parser",
        render_version="none",
        parent_unit="token_window",
        text="original",
    )
    ref, digest = store.put_bytes(
        tenant_ctx=ctx, kind="representation", data=representation.frozen_bytes()
    )
    plan = IndexPlan(
        schema_version=1,
        tenant_id=ctx.tenant_id,
        kb_id=kb,
        document_id=document,
        document_version_id=version,
        representation_id=representation_id,
        index_id=index_id,
        tier="stable",
        source_ref=source_ref,
        source_digest=source_digest,
        representation_ref=ref,
        representation_digest=digest,
        chunker_version="synthetic-chunker",
        chunker_config_digest="a" * 64,
        embedding_profile="synthetic-embedding",
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
    plan_ref, plan_digest = store.put_bytes(
        tenant_ctx=ctx, kind="index-plan", data=plan.frozen_bytes()
    )
    manifest = SimpleNamespace(
        **{
            field: getattr(plan, field)
            for field in ingest._MANIFEST_FIELDS
            if field not in {"id", "plan_ref", "plan_digest", "parser_version", "chunk_total"}
        },
        id=index_id,
        plan_ref=plan_ref,
        plan_digest=plan_digest,
        parser_version=representation.parser_version,
        chunk_total=1,
        indexing_state="pending",
        pg_digest=None,
        vector_digest=None,
        reconciled_at=None,
        error_message=None,
    )
    snapshot = (
        {field: getattr(manifest, field) for field in ingest._MANIFEST_FIELDS},
        dict(
            id=representation_id,
            document_version_id=version,
            representation_ref=ref,
            representation_digest=digest,
            parser_version=representation.parser_version,
            render_version="none",
            parent_unit="token_window",
            page_count=None,
            char_count=8,
        ),
        dict(
            id=version, source_ref=source_ref, source_digest=source_digest, media_type="text/plain"
        ),
    )
    state = dict(manifest=manifest, chunks=[], active=None, in_tx=False, attempt=1, cas_fail=False)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        @contextmanager
        def begin(self):
            before = copy.deepcopy(state)
            state["in_tx"] = True
            try:
                yield
            except Exception:
                state.clear()
                state.update(before)
                raise
            finally:
                state["in_tx"] = False

        def add(self, _row):
            pass

        def flush(self):
            pass

    def guard(_session):
        if state["attempt"] != 1:
            raise ConflictError("stale executor")

    def get_snapshot(_service, _session, requested, expected=None):
        assert requested == index_id
        current = copy.deepcopy(snapshot)
        if expected is not None and expected != current:
            raise ConflictError("contract changed")
        return state["manifest"], current

    class Search:
        def __init__(self, *_args):
            pass

        def upsert_chunks(self, **kwargs):
            state["chunks"] = kwargs["chunks"]

        def read_index_for_reconciliation(self, **_kwargs):
            return [
                projection_from_chunk(c, representation_char_count=8, parent_unit="token_window")
                for c in state["chunks"]
            ]

    class Documents:
        def __init__(self, *_args):
            pass

        def require_by_id(self, _id):
            return SimpleNamespace(active_index_id=state["active"])

        def publish_index(self, **kwargs):
            if state["cas_fail"] or state["active"] != kwargs["expected_active_index_id"]:
                raise ConflictError("CAS changed")
            assert kwargs["expected_projection_digest"] == state["manifest"].pg_digest
            state["active"] = kwargs["index_id"]

    class Manifests:
        def __init__(self, *_args):
            pass

        def mark_failed(self, _id, **kwargs):
            state["manifest"].indexing_state = "failed"
            state["manifest"].error_message = kwargs["error_message"]

    class Vector:
        embedding_profile = "synthetic-embedding"
        embedding_version = "synthetic-model"
        writes = 0
        failure = None
        stale = False
        bad_digest = False

        def upsert(self, **_kwargs):
            assert not state["in_tx"]
            self.writes += 1
            if self.stale:
                state["attempt"] = 2
            if self.failure:
                raise self.failure

        def reconcile(self, **kwargs):
            assert not state["in_tx"]
            if self.bad_digest:
                return "b" * 64
            return projection_digest(
                [
                    projection_from_chunk(
                        c, representation_char_count=8, parent_unit="token_window"
                    )
                    for c in kwargs["chunks"]
                ]
            )

    monkeypatch.setattr(ingest, "bind_session_tenant", lambda *_args: None)
    monkeypatch.setattr(ingest.IngestionService, "_snapshot", get_snapshot)
    monkeypatch.setattr(ingest, "SearchRepository", Search)
    monkeypatch.setattr(ingest, "DocumentRepository", Documents)
    monkeypatch.setattr(ingest, "ChunkManifestRepository", Manifests)
    vector = Vector()
    service = ingest.IngestionService(
        Session, ctx, artifacts=store, vector_writer=vector, execution_guard=guard
    )
    return SimpleNamespace(
        service=service,
        state=state,
        vector=vector,
        plan=plan,
        store=store,
        ctx=ctx,
        version_fields=snapshot[2],
    )


def test_recovery_commits_readbacks_before_publication_and_replay_skips_embedding(recovery):
    r = recovery
    result = r.service.execute(manifest_id=r.plan.index_id)
    assert result.indexing_state == "indexed"
    assert r.state["active"] == r.plan.index_id
    manifest = r.state["manifest"]
    assert manifest.pg_digest == manifest.vector_digest == r.plan.projection_digest(r.ctx)
    assert manifest.reconciled_at is not None
    r.service.execute(manifest_id=r.plan.index_id)
    assert r.vector.writes == 1


def test_partial_vector_failure_preserves_pg_and_plan_and_can_recover(recovery):
    r = recovery
    r.vector.failure = ConnectionError("vector unavailable")
    with pytest.raises(ConnectionError):
        r.service.execute(manifest_id=r.plan.index_id)
    assert r.state["manifest"].indexing_state == "failed"
    assert r.state["manifest"].pg_digest == r.plan.projection_digest(r.ctx)
    assert r.state["manifest"].plan_digest == r.plan.digest()
    assert r.state["active"] is None
    r.vector.failure = None
    r.service.execute(manifest_id=r.plan.index_id)
    assert r.state["active"] == r.plan.index_id


def test_readback_mismatch_never_publishes(recovery):
    r = recovery
    r.vector.bad_digest = True
    with pytest.raises(ConflictError, match="Vector readback"):
        r.service.execute(manifest_id=r.plan.index_id)
    assert r.state["active"] is None
    assert r.state["manifest"].indexing_state == "failed"


def test_stale_executor_does_not_mark_successor_failed(recovery):
    r = recovery
    r.vector.stale = True
    with pytest.raises(ConflictError, match="stale executor"):
        r.service.execute(manifest_id=r.plan.index_id)
    assert r.state["manifest"].indexing_state == "indexing"
    assert r.state["active"] is None


def test_cas_failure_rolls_back_receipts_and_keeps_old_active(recovery):
    r = recovery
    previous = uuid4()
    r.state["active"] = previous
    with pytest.raises(ConflictError, match="CAS changed"):
        r.service.execute(manifest_id=r.plan.index_id)
    assert r.state["active"] == previous
    assert r.state["manifest"].reconciled_at is None
    assert r.state["manifest"].vector_digest is None
    assert r.state["manifest"].indexing_state == "failed"


def test_actual_source_digest_mismatch_prevents_both_store_writes(recovery):
    r = recovery
    path = r.store.root / r.plan.source_ref
    path.write_bytes(b"tampered")
    with pytest.raises(ConflictError, match="artifact digest"):
        r.service.execute(manifest_id=r.plan.index_id)
    assert not r.state["chunks"]
    assert r.vector.writes == 0
    assert r.state["manifest"].indexing_state == "failed"


def test_recovery_refuses_writer_with_a_different_model_version(recovery):
    recovery.vector.embedding_version = "wrong-model-version"
    with pytest.raises(ValidationError, match="embedding profile/version"):
        recovery.service.execute(manifest_id=recovery.plan.index_id)
    assert recovery.vector.writes == 0
    assert recovery.state["active"] is None


def test_recovery_refuses_representation_with_wrong_original_media_type(recovery):
    recovery.version_fields["media_type"] = "application/pdf"
    with pytest.raises(ValidationError, match="original media type"):
        recovery.service.execute(manifest_id=recovery.plan.index_id)
    assert recovery.vector.writes == 0
    assert recovery.state["active"] is None
