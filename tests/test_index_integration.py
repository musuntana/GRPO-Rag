"""Opt-in PG + real artifacts + Qdrant local mechanical index recovery checks.

Hash vectors are explicit test inputs, not semantic retrieval or model evidence.
Faults below are injected boundary failures, not claimed production incidents.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError
from ragos.runtime.index_plan import IndexPlan
from ragos.runtime.index_preparation import IndexPreparationService
from ragos.storage.models.app import ChunkManifest, DocumentRecord
from ragos.storage.repositories.kb import DocumentRepository
from ragos.storage.repositories.search import SearchRepository, SearchTier
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_storage_integration import (
    database,
    execute_fixture,
    nonowner_role,
    parallel_integration_workers,
    prepared_index,
    resources,
    scoped_session,
)

# Import fixtures explicitly: pytest discovers these without a source-path hack.
__all__ = ["database", "nonowner_role"]


@contextmanager
def rejected_sql(connection, expected_state):
    with pytest.raises(DBAPIError) as rejected, connection.begin_nested():
        yield
    assert getattr(rejected.value.orig, "sqlstate", None) == expected_state


def assert_not_public(engine, fixture):
    with scoped_session(engine, fixture.ctx) as session:
        assert (
            SearchRepository(session, fixture.ctx).search(
                tier=SearchTier.STABLE, query="revenue", scope_key=fixture.ctx.scope_key
            )
            == []
        )
        assert session.get(DocumentRecord, fixture.plan.document_id).active_index_id is None


def test_actual_two_store_readback_precedes_publication_and_legacy_stays_hidden(database, tmp_path):
    fixture = prepared_index(database, tmp_path)
    try:
        assert_not_public(database, fixture)
        result = execute_fixture(database, fixture)
        assert result.indexing_state == "indexed"
        expected = fixture.plan.projection_digest(fixture.ctx)
        with scoped_session(database, fixture.ctx) as session:
            manifest = session.get(ChunkManifest, fixture.plan.index_id)
            assert (manifest.pg_digest, manifest.vector_digest) == (expected, expected)
            assert manifest.reconciled_at is not None
            assert (
                session.get(DocumentRecord, fixture.plan.document_id).active_index_id == manifest.id
            )
        legacy_id = uuid4()
        with database.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO search.chunk_stable(id,tenant_id,kb_id,document_id,body) "
                    "VALUES (:id,:tenant,:kb,:doc,'Legacy revenue has no source version')"
                ),
                {
                    "id": legacy_id,
                    "tenant": fixture.ctx.tenant_id,
                    "kb": fixture.plan.kb_id,
                    "doc": fixture.plan.document_id,
                },
            )
        with scoped_session(database, fixture.ctx) as session:
            hits = SearchRepository(session, fixture.ctx).search(
                tier=SearchTier.STABLE, query="revenue", scope_key=fixture.ctx.scope_key
            )
            assert {hit.chunk_id for hit in hits} == {chunk.id for chunk in fixture.plan.chunks}
            assert legacy_id not in {hit.chunk_id for hit in hits}
            assert all(hit.index_id == fixture.plan.index_id for hit in hits)
        # Legacy data was retained rather than removed to make retrieval pass.
        with database.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT document_version_id FROM search.chunk_stable WHERE id=:id"),
                    {"id": legacy_id},
                ).scalar_one()
                is None
            )
    finally:
        fixture.client.close()


def test_vector_written_then_interrupted_plan_recovers_without_false_publication(
    database, tmp_path
):
    fixture = prepared_index(database, tmp_path)

    class InterruptedWriter:
        embedding_profile = fixture.writer.embedding_profile
        embedding_version = fixture.writer.embedding_version

        def upsert(self, **kwargs):
            fixture.writer.upsert(**kwargs)
            raise ConnectionError("injected after the actual vector commit")

        def reconcile(self, **kwargs):
            return fixture.writer.reconcile(**kwargs)

    try:
        with pytest.raises(ConnectionError):
            fixture.service(InterruptedWriter()).execute(manifest_id=fixture.plan.index_id)
        assert_not_public(database, fixture)
        with scoped_session(database, fixture.ctx) as session:
            manifest = session.get(ChunkManifest, fixture.plan.index_id)
            assert manifest.indexing_state == "failed" and manifest.pg_digest is not None
            assert manifest.vector_digest is None and manifest.reconciled_at is None
            assert manifest.plan_ref and manifest.plan_digest
        # Read actual points before retry; this is a completed external stage.
        assert fixture.writer.reconcile(
            tenant_ctx=fixture.ctx,
            tier=SearchTier.STABLE,
            chunks=fixture.plan.as_upserts(fixture.ctx),
        ) == fixture.plan.projection_digest(fixture.ctx)
        execute_fixture(database, fixture)
        with scoped_session(database, fixture.ctx) as session:
            assert session.get(ChunkManifest, fixture.plan.index_id).indexing_state == "indexed"
    finally:
        fixture.client.close()


def test_wrong_vector_readback_cannot_make_a_candidate_active(database, tmp_path):
    fixture = prepared_index(database, tmp_path)

    class IncorrectReceiptWriter:
        embedding_profile = fixture.writer.embedding_profile
        embedding_version = fixture.writer.embedding_version

        def upsert(self, **kwargs):
            fixture.writer.upsert(**kwargs)

        def reconcile(self, **kwargs):
            fixture.writer.reconcile(**kwargs)  # actual readback succeeds first
            return "0" * 64  # deliberate invalid receipt to test the comparison

    try:
        with pytest.raises(ConflictError):
            fixture.service(IncorrectReceiptWriter()).execute(manifest_id=fixture.plan.index_id)
        assert_not_public(database, fixture)
        with scoped_session(database, fixture.ctx) as session:
            manifest = session.get(ChunkManifest, fixture.plan.index_id)
            assert manifest.reconciled_at is None and manifest.indexing_state == "failed"
    finally:
        fixture.client.close()


def test_source_byte_tampering_is_refused_before_any_search_write(database, tmp_path):
    fixture = prepared_index(database, tmp_path)
    try:
        # Modify only this test's new temporary artifact, never a project store.
        source = fixture.artifacts.root / fixture.plan.source_ref
        source.write_bytes(b"replacement source must not be accepted")
        with pytest.raises(ConflictError):
            fixture.service().execute(manifest_id=fixture.plan.index_id)
        assert_not_public(database, fixture)
        with scoped_session(database, fixture.ctx) as session:
            assert (
                session.execute(
                    text("SELECT count(*) FROM search.chunk_stable WHERE index_id=:id"),
                    {"id": fixture.plan.index_id},
                ).scalar_one()
                == 0
            )
    finally:
        fixture.client.close()


def test_publish_cas_preserves_previous_index_after_a_stale_plan(database, tmp_path):
    identifiers = resources(database)
    first = prepared_index(database, tmp_path / "one", identifiers=identifiers)
    stale = prepared_index(
        database, tmp_path / "two", identifiers=identifiers, source_text="Evidence revenue 43"
    )
    try:
        assert first.plan.expected_active_index_id is stale.plan.expected_active_index_id is None
        execute_fixture(database, first)
        with pytest.raises(ConflictError):
            stale.service().execute(manifest_id=stale.plan.index_id)
        with scoped_session(database, first.ctx) as session:
            assert (
                session.get(DocumentRecord, first.plan.document_id).active_index_id
                == first.plan.index_id
            )
            assert session.get(ChunkManifest, stale.plan.index_id).indexing_state == "failed"
            hits = SearchRepository(session, first.ctx).search(
                tier=SearchTier.STABLE, query="revenue", scope_key=first.ctx.scope_key
            )
            assert hits and all(hit.index_id == first.plan.index_id for hit in hits)
    finally:
        first.client.close()
        stale.client.close()


def test_missing_receipts_are_rejected_on_insert_and_update(database, nonowner_role, tmp_path):
    fixture = prepared_index(database, tmp_path)
    try:
        with database.begin() as connection:
            connection.execute(text(f'SET LOCAL ROLE "{nonowner_role}"'))
            connection.execute(
                text("SELECT set_config('app.current_tenant_id',:tid,true)"),
                {"tid": str(fixture.ctx.tenant_id)},
            )
            with rejected_sql(connection, "23514"):
                connection.execute(
                    text("UPDATE app.chunk_manifest SET indexing_state='indexed' WHERE id=:id"),
                    {"id": fixture.plan.index_id},
                )
            with rejected_sql(connection, "23514"):
                connection.execute(
                    text(
                        "INSERT INTO app.chunk_manifest(id,tenant_id,kb_id,document_id,"
                        "document_version_id,representation_id,parent_unit,"
                        "parser_version,chunker_version,"
                        "chunker_config_digest,embedding_profile,embedding_version,"
                        "plan_ref,plan_digest,"
                        "chunk_total,indexing_state,created_at,updated_at) SELECT "
                        ":new_id,tenant_id,kb_id,document_id,"
                        "document_version_id,representation_id,parent_unit,"
                        "parser_version,chunker_version,"
                        "chunker_config_digest,embedding_profile,embedding_version,"
                        "plan_ref,plan_digest,"
                        "chunk_total,'indexed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP "
                        "FROM app.chunk_manifest WHERE id=:id"
                    ),
                    {"new_id": uuid4(), "id": fixture.plan.index_id},
                )
        assert_not_public(database, fixture)
    finally:
        fixture.client.close()


def test_raw_sql_cannot_rewrite_source_plan_or_published_chunk_set(
    database, nonowner_role, tmp_path
):
    fixture = prepared_index(database, tmp_path)
    try:
        execute_fixture(database, fixture)
        with database.begin() as connection:
            connection.execute(text(f'SET LOCAL ROLE "{nonowner_role}"'))
            connection.execute(
                text("SELECT set_config('app.current_tenant_id',:tid,true)"),
                {"tid": str(fixture.ctx.tenant_id)},
            )
            operations = [
                (
                    "UPDATE app.document_version SET source_digest=:digest WHERE id=:id",
                    {"digest": "f" * 64, "id": fixture.plan.document_version_id},
                ),
                (
                    "UPDATE app.document_representation SET char_count=char_count+1 WHERE id=:id",
                    {"id": fixture.plan.representation_id},
                ),
                (
                    "UPDATE app.chunk_manifest SET plan_digest=:digest WHERE id=:id",
                    {"digest": "f" * 64, "id": fixture.plan.index_id},
                ),
                (
                    "UPDATE app.chunk_manifest SET id=:new_id WHERE id=:id",
                    {"new_id": uuid4(), "id": fixture.plan.index_id},
                ),
                (
                    "UPDATE search.chunk_stable SET body='fabricated text' WHERE id=:id",
                    {"id": fixture.plan.chunks[1].id},
                ),
                ("DELETE FROM search.chunk_stable WHERE id=:id", {"id": fixture.plan.chunks[1].id}),
            ]
            for sql, params in operations:
                with rejected_sql(connection, "23514"):
                    connection.execute(text(sql), params)
            with rejected_sql(connection, "23514"):
                connection.execute(
                    text(
                        "INSERT INTO search.chunk_stable(id,tenant_id,kb_id,document_id,"
                        "document_version_id,representation_id,index_id,body,"
                        "start_char_idx,end_char_idx) "
                        "SELECT :new_id,tenant_id,kb_id,document_id,"
                        "document_version_id,representation_id,"
                        "index_id,body,start_char_idx,end_char_idx FROM "
                        "search.chunk_stable WHERE id=:id"
                    ),
                    {"new_id": uuid4(), "id": fixture.plan.chunks[1].id},
                )
        # Genuine indexed replay remains possible without modifying immutable content.
        with scoped_session(database, fixture.ctx) as session:
            SearchRepository(session, fixture.ctx).upsert_chunks(
                tier=SearchTier.STABLE, chunks=fixture.plan.as_upserts(fixture.ctx)
            )
    finally:
        fixture.client.close()


def test_composite_fk_refuses_cross_document_version_ownership(database, nonowner_role, tmp_path):
    first = prepared_index(database, tmp_path / "one")
    other = prepared_index(database, tmp_path / "two")
    try:
        with database.begin() as connection:
            connection.execute(text(f'SET LOCAL ROLE "{nonowner_role}"'))
            connection.execute(
                text("SELECT set_config('app.current_tenant_id',:tid,true)"),
                {"tid": str(first.ctx.tenant_id)},
            )
            with rejected_sql(connection, "23503"):
                connection.execute(
                    text(
                        "INSERT INTO app.document_version(id,tenant_id,kb_id,document_id,"
                        "source_digest,source_ref,media_type,created_at,updated_at) "
                        "VALUES (:id,:tenant,:kb,:foreign_doc,:digest,:ref,"
                        "'text/plain',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": uuid4(),
                        "tenant": first.ctx.tenant_id,
                        "kb": first.plan.kb_id,
                        "foreign_doc": other.plan.document_id,
                        "digest": "e" * 64,
                        "ref": first.plan.source_ref,
                    },
                )
            with rejected_sql(connection, "23503"):
                connection.execute(
                    text(
                        "UPDATE app.document_record SET active_index_id=:foreign_index WHERE id=:id"
                    ),
                    {"foreign_index": other.plan.index_id, "id": first.plan.document_id},
                )
    finally:
        first.client.close()
        other.client.close()


def test_republication_with_wrong_expected_projection_is_refused(database, tmp_path):
    fixture = prepared_index(database, tmp_path)
    try:
        execute_fixture(database, fixture)
        with scoped_session(database, fixture.ctx) as session:
            with pytest.raises(ConflictError):
                DocumentRepository(session, fixture.ctx).publish_index(
                    document_id=fixture.plan.document_id,
                    index_id=fixture.plan.index_id,
                    expected_active_index_id=fixture.plan.index_id,
                    expected_projection_digest="0" * 64,
                )
            assert (
                session.get(DocumentRecord, fixture.plan.document_id).active_index_id
                == fixture.plan.index_id
            )
    finally:
        fixture.client.close()


def equivalent_plan(plan):
    """Change only server identities while preserving the frozen graph semantics."""
    identities = {chunk.id: uuid4() for chunk in plan.chunks}
    data = plan.model_dump()
    data.update(
        index_id=uuid4(),
        chunks=tuple(
            chunk.model_copy(
                update={
                    "id": identities[chunk.id],
                    "parent_chunk_id": identities[chunk.parent_chunk_id]
                    if chunk.parent_chunk_id
                    else None,
                }
            )
            for chunk in plan.chunks
        ),
    )
    return IndexPlan.model_validate(data)


def test_actual_prepare_retry_converges_and_rejects_different_semantics_or_timeout(
    database, tmp_path
):
    fixture = prepared_index(database, tmp_path)
    try:
        preparer = IndexPreparationService(fixture.factory, fixture.ctx, fixture.artifacts)
        retry = preparer.prepare(
            plan=equivalent_plan(fixture.plan), idempotency_key=fixture.preparation_key
        )
        assert retry.manifest_id == fixture.plan.index_id and retry.task_id == fixture.task_id
        changed = fixture.plan.model_copy(update={"embedding_version": "different-fixture-v1"})
        with pytest.raises(ConflictError):
            preparer.prepare(plan=changed, idempotency_key=fixture.preparation_key)
        with pytest.raises(ConflictError):
            preparer.prepare(
                plan=fixture.plan, idempotency_key=fixture.preparation_key, timeout_seconds=30
            )
        with scoped_session(database, fixture.ctx) as session:
            assert (
                session.execute(
                    text("SELECT count(*) FROM app.chunk_manifest WHERE document_id=:doc"),
                    {"doc": fixture.plan.document_id},
                ).scalar_one()
                == 1
            )
    finally:
        fixture.client.close()


def test_concurrent_actual_prepare_same_key_has_one_manifest_and_task(database, tmp_path):
    fixture = prepared_index(database, tmp_path)
    try:
        key = str(uuid4())
        candidates = (equivalent_plan(fixture.plan), equivalent_plan(fixture.plan))
        outcomes = parallel_integration_workers(
            database,
            fixture.ctx,
            "prepare",
            [(plan.model_dump_json(), key) for plan in candidates],
            str(fixture.artifacts.root),
        )
        assert outcomes[0] == outcomes[1]
        winner_index, winner_task = outcomes[0]
        assert winner_index in {str(plan.index_id) for plan in candidates}
        with scoped_session(database, fixture.ctx) as session:
            assert (
                session.execute(
                    text("SELECT count(*) FROM app.chunk_manifest WHERE document_id=:doc"),
                    {"doc": fixture.plan.document_id},
                ).scalar_one()
                == 2
            )  # original precondition + one winner
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM app.task WHERE tenant_id=:tenant AND "
                        "payload->>'manifest_id'=:winner"
                    ),
                    {"tenant": fixture.ctx.tenant_id, "winner": winner_index},
                ).scalar_one()
                == 1
            )
            assert (
                session.execute(
                    text("SELECT id FROM app.task WHERE id=CAST(:id AS uuid)"), {"id": winner_task}
                ).scalar_one()
                is not None
            )
    finally:
        fixture.client.close()
