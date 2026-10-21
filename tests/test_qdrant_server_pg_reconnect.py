"""Opt-in genuine Qdrant HTTP server + PostgreSQL page-retrieval contract.

Both services run in GitHub Actions; a real two-page PDF is extracted with
Poppler and the existing indexed-task recovery/publish machinery is used.
Hash vectors are mechanical test fixtures and NOT model-quality evidence.
The server persists points across *client reconnects*, not across server restarts.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.recall.dense import DenseRecall
from ragos.retrieval.types import RetrieverProfile
from ragos.storage.repositories.search import SearchRepository, SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter, hash_embed
from test_pdf_pg_index_integration import _real_pdf_fixture
from test_storage_integration import database, scoped_session

__all__ = ["database"]


@pytest.fixture
def qdrant_url() -> str:
    """Require explicitly configured *local* disposable Qdrant for writes."""
    from urllib.parse import urlsplit

    value = os.environ.get("RAGOS_TEST_QDRANT_URL")
    if not value:
        pytest.skip("RAGOS_TEST_QDRANT_URL is required for Qdrant server integration")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        pytest.fail("Qdrant server integration may write only to explicit local HTTP")
    client = QdrantClient(url=value, timeout=5)
    try:
        client.get_collections()
    finally:
        client.close()
    return value


def _profile(tenant_id):
    return RetrieverProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        recall_channels=["dense"],
        channel_budgets={"dense": 1.0},
        top_k_recall=10,
    )


async def _hash_query(text, ctx):
    ctx.require_scope_key()
    return hash_embed(text)


def _recall(client, source, collection):
    return DenseRecall(
        client,
        collection,
        _hash_query,
        source=source,
        tier=SearchTier.STABLE,
    )


def test_real_qdrant_http_client_reconnect_and_published_pg_hydration(
    database, qdrant_url, tmp_path
):
    fixture = _real_pdf_fixture(database, tmp_path)
    fixture.client.close()  # The PDF fixture normally uses only in-memory Qdrant.
    first = QdrantClient(url=qdrant_url, timeout=5)
    prefix = "ragos_ci_" + uuid4().hex[:14]
    fixture.client = first
    fixture.writer = QdrantVectorIndexWriter(
        client=first,
        embedding_profile=fixture.plan.embedding_profile,
        embedding_version=fixture.plan.embedding_version,
        collection_prefix=prefix,
        allow_test_embedding=True,
    )
    collection = fixture.writer.collection_name(SearchTier.STABLE)
    reader = None
    try:
        # Simulate a vector write preceding the authorized PostgreSQL CAS.
        # A Qdrant hit alone must NEVER cause a staged PDF page to be disclosed.
        fixture.writer.upsert(
            tenant_ctx=fixture.ctx,
            tier=SearchTier.STABLE,
            chunks=fixture.plan.as_upserts(fixture.ctx),
        )
        with scoped_session(database, fixture.ctx) as session:
            unseen = asyncio.run(
                _recall(first, SearchRepository(session, fixture.ctx), collection).recall(
                    "Contract warranty period is 24 months.",
                    fixture.ctx,
                    _profile(fixture.ctx.tenant_id),
                )
            )
            assert unseen == []

        indexed = fixture.service().execute(manifest_id=fixture.plan.index_id)
        assert indexed.indexing_state == "indexed"
        first.close()

        # New network client, same running Qdrant service and real PG records.
        # This proves server-backed read after disconnect, not node crash recovery.
        reader = QdrantClient(url=qdrant_url, timeout=5)
        from ragos.storage.vector import QdrantVectorIndexWriter as Writer

        readback = Writer(
            client=reader,
            embedding_profile=fixture.plan.embedding_profile,
            embedding_version=fixture.plan.embedding_version,
            collection_prefix=prefix,
            allow_test_embedding=True,
        )
        assert readback.reconcile(
            tenant_ctx=fixture.ctx,
            tier=SearchTier.STABLE,
            chunks=fixture.plan.as_upserts(fixture.ctx),
        ) == fixture.plan.projection_digest(fixture.ctx)

        with scoped_session(database, fixture.ctx) as session:
            hits = asyncio.run(
                _recall(reader, SearchRepository(session, fixture.ctx), collection).recall(
                    "Contract warranty period is 24 months.",
                    fixture.ctx,
                    _profile(fixture.ctx.tenant_id),
                )
            )
        assert len(hits) == 4
        assert {hit.page_no for hit in hits} == {1, 2}
        assert {hit.source_identity.index_id for hit in hits} == {
            fixture.plan.index_id
        }
        assert {hit.text for hit in hits} == {
            "Contract warranty period is 24 months.",
            "Contract retained deposit is 5 percent.",
        }
        assert all(hit.source_text == hit.text for hit in hits)

        # A different tenant cannot see vectors or query the published PG rows.
        foreign = TenantContext(
            tenant_id=uuid4(),
            tenant_slug="other",
            actor_id="synthetic-test",
            trace_id="cross-tenant",
            allowed_kb_ids=[fixture.plan.kb_id],
        )
        with scoped_session(database, foreign) as session:
            other_hits = asyncio.run(
                _recall(reader, SearchRepository(session, foreign), collection).recall(
                    "Contract warranty period is 24 months.",
                    foreign,
                    _profile(foreign.tenant_id),
                )
            )
        assert other_hits == []

        # Even with a known chunk UUID, a changed Qdrant lineage is rejected
        # against authoritative, published SQL evidence.
        target = fixture.plan.as_upserts(fixture.ctx)[1]
        reader.set_payload(
            collection_name=collection,
            points=[readback._point_id(fixture.ctx, SearchTier.STABLE, target)],
            payload={"representation_id": str(uuid4())},
            wait=True,
        )
        with scoped_session(database, fixture.ctx) as session:
            with pytest.raises(ValidationError, match="authoritative source identity"):
                asyncio.run(
                    _recall(reader, SearchRepository(session, fixture.ctx), collection).recall(
                        target.body,
                        fixture.ctx,
                        _profile(fixture.ctx.tenant_id),
                    )
                )
    finally:
        cleanup = reader or QdrantClient(url=qdrant_url, timeout=5)
        try:
            if collection in {c.name for c in cleanup.get_collections().collections}:
                cleanup.delete_collection(collection_name=collection)
        finally:
            cleanup.close()
            first.close()
