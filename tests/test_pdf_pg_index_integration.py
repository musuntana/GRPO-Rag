"""Real PDF -> immutable PostgreSQL plan -> Qdrant recovery integration.

PostgreSQL tests use an explicitly disposable migrated local DSN (the existing
shared fixture); no DSN means skip, never a claim of real persistence.
All PDFs are constructed inside the tests. Embeddings are synthetic hash
vectors and cannot establish semantic model quality.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.parse.layout import LayoutElement, parse_pdf_pages
from ragos.retrieval.parse.pdf_bytes import extract_pdf_bytes
from ragos.runtime.index_preparation import IndexPreparationService
from ragos.runtime.pdf_index import build_pdf_index_plan, freeze_pdf_representation
from ragos.storage.artifacts import ArtifactStore
from ragos.storage.models.app import ChunkManifest, DocumentRecord
from ragos.storage.repositories.kb import (
    DocumentRepository,
    DocumentRepresentationRepository,
    DocumentVersionRepository,
)
from ragos.storage.repositories.search import SearchRepository, SearchTier
from ragos.storage.repositories.tasks import TaskRepository
from ragos.storage.vector import QdrantVectorIndexWriter, build_filter, hash_embed
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session
from test_pdf_byte_extraction import _make_pdf
from test_storage_integration import (
    PreparedFixture,
    context,
    database,
    execute_fixture,
    resources,
    scoped_session,
)

__all__ = ["database"]


def _real_pdf_fixture(engine, tmp_path):
    tenant_id, kb_id, doc_id = resources(engine)
    ctx = context(tenant_id, kb_id)
    pdf = _make_pdf([
        "Contract warranty period is 24 months.",
        "Contract retained deposit is 5 percent.",
    ])
    parsed = extract_pdf_bytes(pdf, title="PDF contract")
    store = ArtifactStore(tmp_path / "artifacts")
    source_ref, source_digest = store.put_bytes(
        tenant_ctx=ctx, kind="source", data=pdf
    )
    with scoped_session(engine, ctx) as session:
        version = DocumentVersionRepository(session, ctx).register(
            kb_id=kb_id,
            document_id=doc_id,
            source_digest=source_digest,
            source_ref=source_ref,
            media_type="application/pdf",
        )
        version_id = version.id
    representation = freeze_pdf_representation(
        parsed, document_version_id=version_id, source_digest=source_digest
    )
    rep_ref, rep_digest = store.put_bytes(
        tenant_ctx=ctx, kind="representation", data=representation.frozen_bytes()
    )
    with scoped_session(engine, ctx) as session:
        record = DocumentRepresentationRepository(session, ctx).register(
            kb_id=kb_id,
            document_id=doc_id,
            document_version_id=version_id,
            representation_digest=rep_digest,
            representation_ref=rep_ref,
            parser_version=representation.parser_version,
            render_version=representation.render_version,
            parent_unit=representation.parent_unit,
            char_count=len(representation.text),
            page_count=len(representation.pages),
        )
        representation_id = record.id
        active = DocumentRepository(session, ctx).require_by_id(doc_id).active_index_id

    plan = build_pdf_index_plan(
        parsed,
        representation=representation,
        tenant_ctx=ctx,
        kb_id=kb_id,
        document_id=doc_id,
        representation_id=representation_id,
        index_id=uuid4(),
        source_ref=source_ref,
        representation_ref=rep_ref,
        representation_digest=rep_digest,
        embedding_profile="hash-test",
        embedding_version="sha256-hash-v1",
        expected_active_index_id=active,
    )
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    prepared = IndexPreparationService(factory, ctx, store).prepare(
        plan=plan, idempotency_key=str(uuid4())
    )
    with scoped_session(engine, ctx) as session:
        claimed = TaskRepository(session, ctx).claim_next(kind="ingest_document")
        assert claimed.id == prepared.task_id

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(
        client=client,
        embedding_profile=plan.embedding_profile,
        embedding_version=plan.embedding_version,
        allow_test_embedding=True,
    )
    return PreparedFixture(
        ctx, store, plan, factory, claimed.id, claimed.attempts, client, writer, "pdf"
    )


def test_real_pdf_persists_published_page_evidence_in_pg_and_qdrant(database, tmp_path):
    fixture = _real_pdf_fixture(database, tmp_path)
    try:
        with scoped_session(database, fixture.ctx) as session:
            doc = session.get(DocumentRecord, fixture.plan.document_id)
            assert doc.active_index_id is None
            assert SearchRepository(session, fixture.ctx).search(
                tier=SearchTier.STABLE,
                query="warranty",
                scope_key=fixture.ctx.scope_key,
            ) == []

        result = execute_fixture(database, fixture)
        assert result.indexing_state == "indexed"
        assert result.chunk_total == len(fixture.plan.chunks) == 4
        expected_digest = fixture.plan.projection_digest(fixture.ctx)

        with scoped_session(database, fixture.ctx) as session:
            manifest = session.get(ChunkManifest, fixture.plan.index_id)
            doc = session.get(DocumentRecord, fixture.plan.document_id)
            assert manifest.pg_digest == manifest.vector_digest == expected_digest
            assert manifest.reconciled_at is not None
            assert doc.active_index_id == fixture.plan.index_id
            hits = SearchRepository(session, fixture.ctx).get_chunks_by_ids(
                tier=SearchTier.STABLE,
                scope_key=fixture.ctx.scope_key,
                chunk_ids=[item.id for item in fixture.plan.chunks],
            )
            assert len(hits) == 4
            assert {h.page_no for h in hits} == {1, 2}
            assert {h.page_count for h in hits} == {2}
            assert all(
                fixture.plan.representation_char_count == h.representation_char_count
                for h in hits
            )
            assert all(
                h.document_version_id == fixture.plan.document_version_id
                and h.representation_id == fixture.plan.representation_id
                and h.index_id == fixture.plan.index_id
                for h in hits
            )
            assert {h.body for h in hits} == {
                "Contract warranty period is 24 months.",
                "Contract retained deposit is 5 percent.",
            }
        response = fixture.client.query_points(
            collection_name=fixture.writer.collection_name(SearchTier.STABLE),
            query=hash_embed("Contract warranty period is 24 months."),
            query_filter=build_filter(
                fixture.ctx.require_scope_key(), kb_ids=fixture.ctx.allowed_kb_ids
            ),
            limit=10,
            with_payload=True,
        )
        assert len(response.points) == 4
        assert all("body" not in (point.payload or {}) for point in response.points)
        assert {point.payload["page_no"] for point in response.points} == {1, 2}

        # Published execution is replayable but not rewritten.
        fixture.service().execute(manifest_id=fixture.plan.index_id)
    finally:
        fixture.client.close()


def test_pdf_source_tamper_prevents_pg_and_qdrant_publication(database, tmp_path):
    fixture = _real_pdf_fixture(database, tmp_path)
    try:
        path = fixture.artifacts.root / fixture.plan.source_ref
        path.write_bytes(b"tampered source after preparation")
        from ragos.core.errors import ConflictError

        with pytest.raises(ConflictError, match="digest"):
            fixture.service().execute(manifest_id=fixture.plan.index_id)
        with scoped_session(database, fixture.ctx) as session:
            doc = session.get(DocumentRecord, fixture.plan.document_id)
            assert doc.active_index_id is None
            assert SearchRepository(session, fixture.ctx).get_chunks_by_ids(
                tier=SearchTier.STABLE,
                scope_key=fixture.ctx.scope_key,
                chunk_ids=[item.id for item in fixture.plan.chunks],
            ) == []
    finally:
        fixture.client.close()


def test_frozen_page_offsets_are_exact_without_database() -> None:
    document = parse_pdf_pages([
        [LayoutElement("text", "Warranty lasts 24 months.", 0)],
        [LayoutElement("text", "Deposit is 5%.", 0)],
    ], parser_version="unit-pdf-v1")
    rep = freeze_pdf_representation(
        document, document_version_id=uuid4(), source_digest="a" * 64
    )
    assert len(rep.pages) == 2
    assert rep.text[rep.pages[0].start:rep.pages[0].end] == "Warranty lasts 24 months."
    assert rep.text[rep.pages[1].start:rep.pages[1].end] == "Deposit is 5%."
    assert rep.pages[0].end + 2 == rep.pages[1].start


def test_plan_builder_rejects_foreign_kb_before_storage() -> None:
    document = parse_pdf_pages([[LayoutElement("text", "valid page", 0)]])
    ctx = TenantContext(
        tenant_id=uuid4(),
        tenant_slug="unit",
        actor_id="server",
        trace_id="unit",
        allowed_kb_ids=[uuid4()],
    )
    version_id = uuid4()
    rep = freeze_pdf_representation(
        document, document_version_id=version_id, source_digest="a" * 64
    )
    with pytest.raises(PermissionDeniedError, match="knowledge base"):
        build_pdf_index_plan(
            document, representation=rep, tenant_ctx=ctx,
            kb_id=uuid4(), document_id=uuid4(), representation_id=uuid4(),
            index_id=uuid4(), source_ref="not-a-ref",
            representation_ref="not-a-ref", representation_digest="0" * 64,
            embedding_profile="hash-test", embedding_version="sha256-hash-v1",
        )


def test_freeze_pdf_fails_closed_on_missing_text() -> None:
    doc = parse_pdf_pages([
        [LayoutElement("text", "first", 0)], [LayoutElement("text", "", 0)]
    ])
    with pytest.raises(ValidationError, match="text on every page"):
        freeze_pdf_representation(
            doc, document_version_id=uuid4(), source_digest="a" * 64
        )
