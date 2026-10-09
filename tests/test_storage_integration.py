"""Opt-in real PostgreSQL checks; fixture creation never substitutes for migrations.

Use RAGOS_TEST_PG_DSN only for a disposable local database already migrated by
its owner. No supplied DSN means explicit skips, not integration acceptance.
"""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from multiprocessing import get_context
from uuid import UUID, uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import ConflictError, PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan, PlannedChunk
from ragos.runtime.index_preparation import IndexPreparationService
from ragos.runtime.ingest import IngestionService
from ragos.runtime.process_runner import ProcessRunner
from ragos.runtime.tasks import TaskCommand, TaskHandlerRegistry, TaskService, process_one_task
from ragos.storage.artifacts import ArtifactStore, canonical_json
from ragos.storage.db import REQUIRED_SCHEMA_REVISION, bind_session_tenant
from ragos.storage.models.app import DocumentRecord, KnowledgeBase, Task, Tenant
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.kb import (
    DocumentRepository,
    DocumentRepresentationRepository,
    DocumentVersionRepository,
)
from ragos.storage.repositories.search import SearchChunkUpsert, SearchRepository, SearchTier
from ragos.storage.repositories.tasks import (
    DEFAULT_MAX_ATTEMPTS,
    TaskRepository,
)
from ragos.storage.vector import QdrantVectorIndexWriter, build_filter, hash_embed
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, create_engine


@pytest.fixture
def database():
    dsn = os.environ.get("RAGOS_TEST_PG_DSN")
    if not dsn:
        pytest.skip("RAGOS_TEST_PG_DSN must explicitly identify a disposable local PostgreSQL DB")
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql" or url.host not in {
        None,
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("integration writes require an explicitly disposable local PostgreSQL DSN")
    if url.database in {None, "postgres", "template0", "template1"}:
        pytest.fail("integration writes cannot target a PostgreSQL maintenance database")
    engine = create_engine(
        dsn,
        pool_timeout=3,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=5000 -c lock_timeout=1000",
        },
    )
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM app.alembic_version")
            ).scalar_one()
            assert revision == REQUIRED_SCHEMA_REVISION, "requires this release migration revision"
        yield engine
    finally:
        engine.dispose()


def resources(engine):
    tenant = Tenant(slug=f"test-{uuid4()}", display_name="Validation")
    with Session(engine) as session, session.begin():
        session.add(tenant)
        session.flush()
        kb = KnowledgeBase(tenant_id=tenant.id, slug="kb", name="KB")
        session.add(kb)
        session.flush()
        doc = DocumentRecord(tenant_id=tenant.id, kb_id=kb.id, title="Source")
        session.add(doc)
        session.flush()
        identifiers = tenant.id, kb.id, doc.id
    return identifiers


def context(tenant_id, kb_id, *, user_id=None):
    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug="test",
        actor_id=str(user_id or "worker"),
        user_id=user_id,
        allowed_kb_ids=[kb_id],
        trace_id="integration",
    )


@contextmanager
def scoped_session(engine, ctx):
    with Session(engine, expire_on_commit=False) as session:
        bind_session_tenant(session, ctx.tenant_id)
        with session.begin():
            yield session


@dataclass
class PreparedFixture:
    ctx: TenantContext
    artifacts: ArtifactStore
    plan: IndexPlan
    factory: object
    task_id: UUID
    attempts: int
    client: QdrantClient
    writer: QdrantVectorIndexWriter
    preparation_key: str

    def guard(self, session):
        TaskRepository(session, self.ctx).lock_claim(self.task_id, expected_attempts=self.attempts)

    def service(self, writer=None):
        return IngestionService(
            self.factory,
            self.ctx,
            artifacts=self.artifacts,
            vector_writer=writer or self.writer,
            execution_guard=self.guard,
        )


def prepared_index(
    engine, tmp_path, *, identifiers=None, source_text="Evidence revenue 42", child_id=None
):
    """Actual plain bytes and their exact representation; no pretend parser/model.

    Registration supplies actual source preconditions; product preparation
    creates the frozen manifest and task. The guard is a genuinely claimed DB
    task, never a no-op. This does not demonstrate an automatic parser.
    """
    identifiers = identifiers or resources(engine)
    tenant_id, kb_id, doc_id = identifiers
    ctx = context(tenant_id, kb_id)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source_ref, source_digest = artifacts.put_bytes(
        tenant_ctx=ctx, kind="source", data=source_text.encode("utf-8")
    )
    with scoped_session(engine, ctx) as session:
        version = DocumentVersionRepository(session, ctx).register(
            kb_id=kb_id,
            document_id=doc_id,
            source_digest=source_digest,
            source_ref=source_ref,
            media_type="text/plain",
        )
        version_id = version.id
    representation = FrozenRepresentation(
        schema_version=1,
        document_version_id=version_id,
        source_digest=source_digest,
        source_type="plain",
        parser_version="fixture-plain-v1",
        render_version="none",
        parent_unit="token_window",
        text=source_text,
    )
    representation_ref, representation_digest = artifacts.put_bytes(
        tenant_ctx=ctx, kind="representation", data=representation.frozen_bytes()
    )
    with scoped_session(engine, ctx) as session:
        registered = DocumentRepresentationRepository(session, ctx).register(
            kb_id=kb_id,
            document_id=doc_id,
            document_version_id=version_id,
            representation_digest=representation_digest,
            representation_ref=representation_ref,
            parser_version=representation.parser_version,
            render_version=representation.render_version,
            parent_unit="token_window",
            char_count=len(source_text),
        )
        representation_id = registered.id
        expected_active = DocumentRepository(session, ctx).require_by_id(doc_id).active_index_id
    parent_id, index_id = uuid4(), uuid4()
    child_start = source_text.index("revenue")
    plan = IndexPlan(
        schema_version=1,
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=doc_id,
        document_version_id=version_id,
        representation_id=representation_id,
        index_id=index_id,
        tier="stable",
        source_ref=source_ref,
        source_digest=source_digest,
        representation_ref=representation_ref,
        representation_digest=representation_digest,
        chunker_version="fixture-span-v1",
        chunker_config_digest=hashlib.sha256(
            canonical_json({"parent": "full-text", "child": "revenue-to-end"})
        ).hexdigest(),
        embedding_profile="hash-test",
        embedding_version="sha256-hash-v1",
        parent_unit="token_window",
        representation_char_count=len(source_text),
        expected_active_index_id=expected_active,
        chunks=(
            PlannedChunk(
                id=parent_id,
                parent_chunk_id=None,
                is_parent=True,
                body=source_text,
                start_char_idx=0,
                end_char_idx=len(source_text),
                element_type="text",
            ),
            PlannedChunk(
                id=child_id or uuid4(),
                parent_chunk_id=parent_id,
                is_parent=False,
                body=source_text[child_start:],
                start_char_idx=child_start,
                end_char_idx=len(source_text),
                element_type="text",
            ),
        ),
    )
    plan.verify_representation(representation)
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    preparation_key = str(uuid4())
    prepared = IndexPreparationService(factory, ctx, artifacts).prepare(
        plan=plan, idempotency_key=preparation_key
    )
    assert prepared.manifest_id == plan.index_id
    with scoped_session(engine, ctx) as session:
        claimed = TaskRepository(session, ctx).claim_next(kind="ingest_document")
        assert claimed.id == prepared.task_id
        task_id, attempts = claimed.id, claimed.attempts
    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(
        client=client,
        allow_test_embedding=True,
        embedding_profile=plan.embedding_profile,
        embedding_version=plan.embedding_version,
    )
    return PreparedFixture(
        ctx, artifacts, plan, factory, task_id, attempts, client, writer, preparation_key
    )


def execute_fixture(engine, fixture):
    result = fixture.service().execute(manifest_id=fixture.plan.index_id)
    with scoped_session(engine, fixture.ctx) as session:
        TaskRepository(session, fixture.ctx).mark_completed(
            fixture.task_id, expected_attempts=fixture.attempts
        )
    return result


def memory_chunk(ctx, *, chunk_id=None):
    return SearchChunkUpsert(
        id=chunk_id or uuid4(),
        tenant_id=ctx.tenant_id,
        kb_id=None,
        document_id=None,
        parent_chunk_id=None,
        layer="memory",
        lang="simple",
        title=None,
        headings_text=None,
        body="Private preference",
        tags_text=None,
        metadata_json={},
        updated_at=utc_now(),
        user_id=ctx.require_user_id(),
    )


@pytest.fixture
def nonowner_role(database):
    role = "s0_probe_" + uuid4().hex
    with database.begin() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA app, search TO "{role}"'))
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app, search "
                f'TO "{role}"'
            )
        )
    try:
        yield role
    finally:
        # Revoke only grants made to this freshly created role; no business rows are deleted.
        with database.begin() as connection:
            connection.execute(
                text(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA app, search FROM "{role}"')
            )
            connection.execute(text(f'REVOKE ALL PRIVILEGES ON SCHEMA app, search FROM "{role}"'))
            connection.execute(text(f'DROP ROLE "{role}"'))


def test_real_search_vector_hydration_and_collision_isolation(database, tmp_path):
    first = prepared_index(database, tmp_path / "first")
    second = prepared_index(database, tmp_path / "second", child_id=first.plan.chunks[1].id)
    try:
        execute_fixture(database, first)
        with scoped_session(database, first.ctx) as session:
            repo = SearchRepository(session, first.ctx)
            hits = repo.search(
                tier=SearchTier.STABLE, query="revenue", scope_key=first.ctx.scope_key
            )
            assert {hit.chunk_id for hit in hits} == {chunk.id for chunk in first.plan.chunks}
            for hit in hits:
                assert (hit.document_version_id, hit.representation_id, hit.index_id) == (
                    first.plan.document_version_id,
                    first.plan.representation_id,
                    first.plan.index_id,
                )
                assert hit.body == "Evidence revenue 42"[hit.start_char_idx : hit.end_char_idx]
                assert hit.parent_unit == "token_window" and hit.page_no is None
                assert hit.representation_char_count == len("Evidence revenue 42")
            assert repo.count_children(
                tier=SearchTier.STABLE,
                scope_key=first.ctx.scope_key,
                parent_ids=[first.plan.chunks[0].id],
            ) == {first.plan.chunks[0].id: 1}
        with scoped_session(database, second.ctx) as session:
            assert (
                SearchRepository(session, second.ctx).get_chunks_by_ids(
                    tier=SearchTier.STABLE,
                    scope_key=second.ctx.scope_key,
                    chunk_ids=[first.plan.chunks[1].id],
                )
                == []
            )
            with pytest.raises(PermissionDeniedError):
                SearchRepository(session, second.ctx).upsert_chunks(
                    tier=SearchTier.STABLE, chunks=second.plan.as_upserts(second.ctx)
                )
            # The internal savepoint also rolls back the nonconflicting parent.
            assert (
                session.execute(
                    text("SELECT count(*) FROM search.chunk_stable WHERE index_id=:id"),
                    {"id": second.plan.index_id},
                ).scalar_one()
                == 0
            )
        first.writer.upsert(
            tenant_ctx=second.ctx, tier=SearchTier.STABLE, chunks=second.plan.as_upserts(second.ctx)
        )
        found = first.client.query_points(
            collection_name=first.writer.collection_name(SearchTier.STABLE),
            query=hash_embed("revenue"),
            query_filter=build_filter(first.ctx.scope_key, kb_ids=first.ctx.allowed_kb_ids),
            with_payload=True,
            limit=10,
        )
        assert len(found.points) == len(first.plan.chunks)
        assert all(point.payload["index_id"] == str(first.plan.index_id) for point in found.points)
    finally:
        first.client.close()
        second.client.close()


def test_real_memory_uuid_conflict_cannot_change_owner(database):
    tenant_id, kb_id, _ = resources(database)
    owner1, owner2 = uuid4(), uuid4()
    ctx1, ctx2 = (
        context(tenant_id, kb_id, user_id=owner1),
        context(tenant_id, kb_id, user_id=owner2),
    )
    item = memory_chunk(ctx1)
    with scoped_session(database, ctx1) as session:
        SearchRepository(session, ctx1).upsert_chunks(tier=SearchTier.MEMORY, chunks=[item])
    with scoped_session(database, ctx2) as session:
        with pytest.raises(PermissionDeniedError):
            SearchRepository(session, ctx2).upsert_chunks(
                tier=SearchTier.MEMORY, chunks=[memory_chunk(ctx2, chunk_id=item.id)]
            )
        assert (
            SearchRepository(session, ctx2).get_chunks_by_ids(
                tier=SearchTier.MEMORY,
                scope_key=ctx2.require_private_scope_key(),
                chunk_ids=[item.id],
            )
            == []
        )
    with scoped_session(database, ctx1) as session:
        hit = SearchRepository(session, ctx1).get_chunks_by_ids(
            tier=SearchTier.MEMORY, scope_key=ctx1.require_private_scope_key(), chunk_ids=[item.id]
        )[0]
        assert hit.body == item.body and hit.user_id == owner1
        assert hit.document_version_id is None and hit.page_no is None


def test_real_task_reclaim_fences_previous_attempt_and_filters_kinds(database):
    tenant_id, kb_id, _ = resources(database)
    ctx = context(tenant_id, kb_id)
    kind = "validation_" + uuid4().hex
    with scoped_session(database, ctx) as session:
        repo = TaskRepository(session, ctx)
        task = repo.enqueue(kind=kind, payload={}, idempotency_key=str(uuid4()))
        task_id = task.id
        repo.enqueue(kind="unhandled", payload={}, idempotency_key=str(uuid4()))
    with scoped_session(database, ctx) as session:
        claimed = TaskRepository(session, ctx).claim_next(kind=kind, lease_seconds=1)
        assert claimed.id == task_id
        original_deadline = claimed.deadline_at
        claimed.lease_until = utc_now() - timedelta(seconds=1)  # injected worker crash/expiry
    with scoped_session(database, ctx) as session:
        with pytest.raises(ConflictError):
            TaskRepository(session, ctx).mark_completed(task_id, expected_attempts=1)
    with scoped_session(database, ctx) as session:
        claimed = TaskRepository(session, ctx).claim_next(kind=kind)
        assert claimed.id == task_id and claimed.attempts == 2
        assert claimed.deadline_at == original_deadline
    with scoped_session(database, ctx) as session:
        with pytest.raises(ConflictError):
            TaskRepository(session, ctx).mark_completed(task_id, expected_attempts=1)
        assert (
            TaskRepository(session, ctx).mark_completed(task_id, expected_attempts=2).status
            == "completed"
        )


def test_real_task_duplicate_key_binds_payload_and_timeout(database):
    tenant_id, kb_id, _ = resources(database)
    ctx, key = context(tenant_id, kb_id), str(uuid4())
    with scoped_session(database, ctx) as session:
        repo = TaskRepository(session, ctx)
        task = repo.enqueue(
            kind="validation", payload={"input": 1}, idempotency_key=key, timeout_seconds=30
        )
        assert (
            repo.enqueue(
                kind="validation", payload={"input": 1}, idempotency_key=key, timeout_seconds=30
            ).id
            == task.id
        )
        for payload, timeout in (({"input": 2}, 30), ({"input": 1}, 300)):
            with pytest.raises(ConflictError):
                repo.enqueue(
                    kind="validation", payload=payload, idempotency_key=key, timeout_seconds=timeout
                )


def test_real_rls_scope_binding_preserves_service_transaction_and_pool_reuse(database):
    first, second = resources(database), resources(database)
    ctx = context(*first[:2])
    with Session(database, expire_on_commit=False) as session:
        bind_session_tenant(session, ctx.tenant_id)
        assert not session.in_transaction()
        assert (
            TaskService(session, ctx)
            .enqueue(kind="validation", payload={}, idempotency_key=str(uuid4()))
            .tenant_id
            == ctx.tenant_id
        )
        with session.begin():
            assert session.execute(
                text("SELECT current_setting('app.current_tenant_id')")
            ).scalar_one() == str(ctx.tenant_id)
    with Session(database, expire_on_commit=False) as session:
        bind_session_tenant(session, second[0])
        with session.begin():
            assert session.execute(
                text("SELECT current_setting('app.current_tenant_id')")
            ).scalar_one() == str(second[0])


def test_real_rls_nonowner_refuses_missing_invalid_and_foreign_scope(
    database, nonowner_role, tmp_path
):
    first, second = (
        prepared_index(database, tmp_path / "one"),
        prepared_index(database, tmp_path / "two"),
    )
    try:
        execute_fixture(database, first)
        execute_fixture(database, second)
        with database.begin() as connection:
            connection.execute(text(f'SET LOCAL ROLE "{nonowner_role}"'))
            for scope in ("", "invalid-uuid", str(uuid4())):
                connection.execute(
                    text("SELECT set_config('app.current_tenant_id', :tid, true)"), {"tid": scope}
                )
                for table in (
                    "app.knowledge_base",
                    "app.document_version",
                    "app.document_representation",
                    "app.chunk_manifest",
                    "search.chunk_stable",
                ):
                    assert connection.execute(text(f"SELECT id FROM {table}")).all() == []
            connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(first.ctx.tenant_id)},
            )
            assert set(
                connection.execute(text("SELECT id FROM search.chunk_stable")).scalars()
            ) == {item.id for item in first.plan.chunks}
            assert list(
                connection.execute(text("SELECT id FROM app.knowledge_base")).scalars()
            ) == [first.plan.kb_id]
            with pytest.raises(DBAPIError) as rejected, connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO search.memory(id,tenant_id,body) VALUES (:id,:tid,'forbidden')"
                    ),
                    {"id": uuid4(), "tid": second.ctx.tenant_id},
                )
            assert getattr(rejected.value.orig, "sqlstate", None) == "42501"
    finally:
        first.client.close()
        second.client.close()


def test_real_dispatch_uses_cpu_process_heartbeats_and_no_business_row_lock(database):
    tenant_id, kb_id, _ = resources(database)
    ctx, kind = context(tenant_id, kb_id), "process_" + uuid4().hex
    with scoped_session(database, ctx) as session:
        task_id = (
            TaskRepository(session, ctx)
            .enqueue(kind=kind, payload={}, idempotency_key=str(uuid4()), timeout_seconds=10)
            .id
        )
    factory = sessionmaker(database, class_=Session, expire_on_commit=False)
    registry, observed, renewed = TaskHandlerRegistry(), [], []

    def command(*, task, tenant_ctx):
        assert task.id == task_id and task.tenant_id == tenant_ctx.tenant_id == tenant_id
        with scoped_session(database, ctx) as session:
            # This would raise a lock error if dispatcher retained its preparation lock.
            row = session.execute(
                select(Task).where(Task.id == task_id).with_for_update(nowait=True)
            ).scalar_one()
            observed.append(row.lease_until)
        return TaskCommand(
            (sys.executable, "-c", "import time; sum(range(200000)); time.sleep(2.2)"), {}
        )

    class ObservingRunner(ProcessRunner):
        def run(self, *args, **kwargs):
            original = kwargs["heartbeat"]

            def heartbeat():
                original()
                with scoped_session(database, ctx) as session:
                    row = session.get(Task, task_id)
                    renewed.append(row.lease_until)
                    assert row.status == "leased"

            kwargs["heartbeat"] = heartbeat
            return super().run(*args, **kwargs)

    registry.register(kind, command)
    assert process_one_task(factory, registry, lease_seconds=2, runner=ObservingRunner())
    assert observed and renewed and max(renewed) > observed[0]
    with scoped_session(database, ctx) as session:
        row = session.get(Task, task_id)
        assert row.status == "completed" and row.finished_at is not None
        assert row.attempts == 1 and row.error_code is None and row.lease_until is None


def test_real_repeated_crashes_reach_failed_after_attempt_cap(database):
    tenant_id, kb_id, _ = resources(database)
    ctx, kind = context(tenant_id, kb_id), "crash_" + uuid4().hex
    with scoped_session(database, ctx) as session:
        task_id = (
            TaskRepository(session, ctx)
            .enqueue(kind=kind, payload={}, idempotency_key=str(uuid4()))
            .id
        )
    original_deadline = None
    for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        with scoped_session(database, ctx) as session:
            task = TaskRepository(session, ctx).claim_next(kind=kind)
            assert task.id == task_id and task.attempts == attempt
            original_deadline = original_deadline or task.deadline_at
            assert task.deadline_at == original_deadline
            task.lease_until = utc_now() - timedelta(seconds=1)  # explicit crash injection
    with scoped_session(database, ctx) as session:
        assert TaskRepository(session, ctx).claim_next(kind=kind) is None
        task = session.get(Task, task_id)
        assert task.status == "failed" and task.error_code == "TASK_ATTEMPTS_EXHAUSTED"
        assert task.finished_at is not None and task.deadline_at == original_deadline


def integration_worker(
    dsn, tenant_id, kb_id, barrier, results, operation, argument, artifact_root=None
):
    """Dedicated test child; never reuses a forked parent's PG connection pool."""
    engine = create_engine(
        dsn,
        pool_timeout=3,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=5000 -c lock_timeout=1000",
        },
    )
    try:
        ctx = context(UUID(tenant_id), UUID(kb_id))
        barrier.wait(timeout=5)
        if operation == "claim":
            with scoped_session(engine, ctx) as session:
                task = TaskRepository(session, ctx).claim_next(kind=argument)
                value = str(task.id) if task else None
        elif operation == "prepare":
            plan_json, key = argument
            plan = IndexPlan.model_validate_json(plan_json)
            factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
            prepared = IndexPreparationService(factory, ctx, ArtifactStore(artifact_root)).prepare(
                plan=plan, idempotency_key=key
            )
            value = (str(prepared.manifest_id), str(prepared.task_id))
        else:
            raise ValueError("unknown controlled test operation")
        results.put(("ok", value))
    except BaseException as exc:
        results.put(("error", type(exc).__name__))
    finally:
        engine.dispose()


def parallel_integration_workers(database, ctx, operation, arguments, artifact_root=None):
    """Bounded processes, not a thread pool with an unbounded shutdown join."""
    if sys.platform != "linux":
        pytest.skip("bounded storage race probes currently require Linux")
    process_context = get_context("fork")
    barrier = process_context.Barrier(len(arguments))
    results = process_context.Queue()
    processes = [
        process_context.Process(
            target=integration_worker,
            args=(
                database.url.render_as_string(hide_password=False),
                str(ctx.tenant_id),
                str(ctx.allowed_kb_ids[0]),
                barrier,
                results,
                operation,
                argument,
                artifact_root,
            ),
        )
        for argument in arguments
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=12)
        assert not any(process.is_alive() for process in processes), (
            "storage race exceeded its process budget"
        )
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=2) for _ in processes]
        assert all(status == "ok" for status, _ in outcomes), outcomes
        return [value for _, value in outcomes]
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            if process.pid is not None and not process.is_alive():
                process.close()
        results.close()


def test_real_concurrent_workers_claim_one_generation(database):
    tenant_id, kb_id, _ = resources(database)
    ctx, kind = context(tenant_id, kb_id), "race_" + uuid4().hex
    with scoped_session(database, ctx) as session:
        task_id = (
            TaskRepository(session, ctx)
            .enqueue(kind=kind, payload={}, idempotency_key=str(uuid4()))
            .id
        )
    results = parallel_integration_workers(database, ctx, "claim", [kind, kind])
    assert results.count(str(task_id)) == 1 and results.count(None) == 1
    with scoped_session(database, ctx) as session:
        assert session.get(Task, task_id).attempts == 1


def test_real_cancel_intent_fences_completion_then_process_ack(database):
    tenant_id, kb_id, _ = resources(database)
    ctx, kind = context(tenant_id, kb_id), "cancel_" + uuid4().hex
    with scoped_session(database, ctx) as session:
        task_id = (
            TaskRepository(session, ctx)
            .enqueue(kind=kind, payload={}, idempotency_key=str(uuid4()))
            .id
        )
        task = TaskRepository(session, ctx).claim_next(kind=kind)
        attempt = task.attempts
    with scoped_session(database, ctx) as session:
        task = TaskRepository(session, ctx).request_cancel(task_id)
        assert task.status == "leased" and task.finished_at is None
    with scoped_session(database, ctx) as session:
        repo = TaskRepository(session, ctx)
        with pytest.raises(ConflictError):
            repo.mark_completed(task_id, expected_attempts=attempt)
        with pytest.raises(ConflictError):
            repo.renew_lease(task_id, expected_attempts=attempt)
        task = repo.mark_cancelled(task_id, expected_attempts=attempt)
        assert task.status == "cancelled" and task.finished_at is not None
