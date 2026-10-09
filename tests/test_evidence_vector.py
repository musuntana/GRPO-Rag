"""Synthetic lineage fixtures verify contracts, not semantic retrieval quality."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter, build_filter


def chunk(tenant_id):
    return SearchChunkUpsert(
        id=uuid4(),
        tenant_id=tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        start_char_idx=0,
        end_char_idx=len("synthetic source"),
        representation_char_count=100,
        parent_unit="token_window",
        parent_chunk_id=None,
        layer="L1",
        lang="simple",
        title=None,
        headings_text=None,
        body="synthetic source",
        tags_text=None,
        metadata_json={},
        updated_at=datetime.now(UTC),
    )


def context(tenant_id, **kwargs):
    return TenantContext(
        tenant_id=tenant_id, tenant_slug="test", actor_id="owner", trace_id="synthetic", **kwargs
    )


@pytest.mark.parametrize(
    "field", ["document_version_id", "representation_id", "index_id", "document_id", "kb_id"]
)
def test_missing_enterprise_identity_rejected_before_network(field):
    source = chunk(uuid4())
    writer = QdrantVectorIndexWriter(client=object(), allow_test_embedding=True)
    with pytest.raises(PermissionDeniedError, match="complete source identity"):
        writer.upsert(
            tenant_ctx=context(source.tenant_id),
            tier=SearchTier.STABLE,
            chunks=[replace(source, **{field: None})],
        )


def test_invalid_embedding_rejected_before_network():
    source = chunk(uuid4())
    writer = QdrantVectorIndexWriter(
        client=object(),
        embedding_fn=lambda _: [float("nan")],
        embedding_dim=1,
        embedding_version="explicit-nonfinite-test",
    )
    with pytest.raises(ValueError, match="non-finite"):
        writer.upsert(tenant_ctx=context(source.tenant_id), tier=SearchTier.STABLE, chunks=[source])


def test_shadow_index_points_do_not_overwrite_and_legacy_is_not_read():
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(client=client, allow_test_embedding=True)
    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    writer.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    writer.upsert(
        tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[replace(source, index_id=uuid4())]
    )
    name = writer.collection_name(SearchTier.STABLE)
    client.upsert(
        collection_name=name,
        points=[
            PointStruct(
                id=str(uuid4()),
                vector=[1.0] * writer.embedding_dim,
                payload={"tenant_id": str(source.tenant_id), "chunk_id": str(uuid4())},
            )
        ],
    )
    points, _ = client.scroll(
        collection_name=name, scroll_filter=build_filter(ctx.require_scope_key()), limit=10
    )
    assert len(points) == 2
    assert len({point.id for point in points}) == 2
    assert {point.payload["chunk_id"] for point in points} == {str(source.id)}
    for point in points:
        assert point.payload["document_version_id"] == str(source.document_version_id)
        assert point.payload["representation_id"] == str(source.representation_id)
    client.close()


def test_memory_has_owner_namespace_without_enterprise_identity():
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(client=client, allow_test_embedding=True)
    source = chunk(uuid4())
    user_a, user_b = uuid4(), uuid4()
    memory = replace(
        source,
        document_version_id=None,
        representation_id=None,
        index_id=None,
        representation_char_count=None,
        parent_unit=None,
        user_id=user_a,
    )
    writer.upsert(
        tenant_ctx=context(source.tenant_id, user_id=user_a),
        tier=SearchTier.MEMORY,
        chunks=[memory],
    )
    writer.upsert(
        tenant_ctx=context(source.tenant_id, user_id=user_b),
        tier=SearchTier.MEMORY,
        chunks=[replace(memory, user_id=user_b)],
    )
    assert client.count(collection_name=writer.collection_name(SearchTier.MEMORY)).count == 2
    with pytest.raises(PermissionDeniedError, match="memory owner"):
        writer.upsert(
            tenant_ctx=context(source.tenant_id, user_id=user_b),
            tier=SearchTier.MEMORY,
            chunks=[memory],
        )
    client.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"representation_char_count": None},
        {"parent_unit": None},
        {"parent_unit": "page", "page_count": None},
        {"page_count": 1},
    ],
)
def test_unvalidated_representation_rejected_before_network(changes):
    source = chunk(uuid4())
    writer = QdrantVectorIndexWriter(client=object(), allow_test_embedding=True)
    with pytest.raises(PermissionDeniedError, match="representation bounds"):
        writer.upsert(
            tenant_ctx=context(source.tenant_id),
            tier=SearchTier.STABLE,
            chunks=[replace(source, **changes)],
        )


def test_reconcile_reads_persisted_projection_without_embedding():
    from qdrant_client import QdrantClient
    from ragos.storage.index_projection import projection_digest, projection_from_chunk

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(client=client, allow_test_embedding=True)
    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    writer.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])

    def forbidden_embedding(_):
        raise AssertionError("readback must not invoke model")

    writer.embedding_fn = forbidden_embedding
    digest = writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    assert digest == projection_digest(
        [projection_from_chunk(source, representation_char_count=100, parent_unit="token_window")]
    )
    client.close()


@pytest.mark.parametrize("corruption", ["missing", "body_digest", "page_offset_start", "extra"])
def test_reconcile_rejects_incomplete_or_changed_persisted_index(corruption):
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    from ragos.core.errors import ValidationError

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(client=client, allow_test_embedding=True)
    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    writer.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    name = writer.collection_name(SearchTier.STABLE)
    point_id = writer._point_id(ctx, SearchTier.STABLE, source)
    if corruption == "missing":
        client.delete(collection_name=name, points_selector=[point_id])
    elif corruption == "extra":
        points = client.retrieve(collection_name=name, ids=[point_id], with_vectors=True)
        client.upsert(
            collection_name=name,
            points=[
                PointStruct(id=str(uuid4()), vector=points[0].vector, payload=points[0].payload)
            ],
        )
    else:
        value = "0" * 64 if corruption == "body_digest" else 1
        client.set_payload(collection_name=name, payload={corruption: value}, points=[point_id])
    with pytest.raises(ValidationError):
        writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    client.close()


def test_reconcile_rejects_memory_empty_and_duplicate_plan_before_io():
    from ragos.core.errors import ValidationError

    writer = QdrantVectorIndexWriter(client=object(), allow_test_embedding=True)
    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    with pytest.raises(PermissionDeniedError, match="memory"):
        writer.reconcile(tenant_ctx=ctx, tier=SearchTier.MEMORY, chunks=[])
    with pytest.raises(ValidationError, match="nonempty"):
        writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[])
    with pytest.raises(ValidationError, match="unique"):
        writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source, source])


@pytest.mark.parametrize(
    "mode", ["duplicate", "wrong-id", "empty-vector", "nonfinite", "wrong-dimension"]
)
def test_reconcile_rejects_invalid_actual_point_records(mode):
    from types import SimpleNamespace

    from ragos.core.errors import ValidationError
    from ragos.storage.index_projection import projection_from_chunk
    from ragos.storage.vector import _chunk_payload

    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    payload = _chunk_payload(source)
    payload.update(
        projection_from_chunk(
            source, representation_char_count=100, parent_unit="token_window"
        ).model_dump(mode="json")
    )
    writer = QdrantVectorIndexWriter(client=object(), allow_test_embedding=True)
    point = SimpleNamespace(
        id=writer._point_id(ctx, SearchTier.STABLE, source),
        payload=payload,
        vector=[0.1] * writer.embedding_dim,
    )
    records = [point]
    if mode == "duplicate":
        records.append(point)
    elif mode == "wrong-id":
        point.id = str(uuid4())
    elif mode == "empty-vector":
        point.vector = []
    elif mode == "nonfinite":
        point.vector[0] = float("nan")
    else:
        point.vector = [0.1]

    class ReadbackClient:
        def retrieve(self, **kwargs):
            assert kwargs["with_vectors"] is True
            return records

    writer.client = ReadbackClient()
    with pytest.raises((ValidationError, ValueError)):
        writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])


def test_same_profile_different_version_uses_distinct_physical_collection():
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    source = chunk(uuid4())
    ctx = context(source.tenant_id)
    first = QdrantVectorIndexWriter(
        client=client,
        allow_test_embedding=True,
        embedding_profile="synthetic",
        embedding_version="v1",
    )
    second = QdrantVectorIndexWriter(
        client=client,
        allow_test_embedding=True,
        embedding_profile="synthetic",
        embedding_version="v2",
    )
    first.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    assert first.collection_name(SearchTier.STABLE) != second.collection_name(SearchTier.STABLE)
    with pytest.raises(ValueError):
        second.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    second.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=[source])
    assert len(client.get_collections().collections) == 2
    client.close()
