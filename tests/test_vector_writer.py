from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from ragos.core.errors import PermissionDeniedError
from ragos.core.tenant import ActorType, TenantContext
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier
from ragos.storage.vector import (
    HASH_EMBED_DIM,
    QdrantVectorIndexWriter,
    _chunk_payload,
    hash_embed,
)


def _upsert(
    tenant_id: UUID,
    body: str = "hello",
    chunk_id: UUID | None = None,
) -> SearchChunkUpsert:
    return SearchChunkUpsert(
        id=chunk_id or uuid4(),
        tenant_id=tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        start_char_idx=0,
        end_char_idx=len(body),
        representation_char_count=100,
        parent_unit="token_window",
        parent_chunk_id=None,
        layer="L1",
        lang="simple",
        title="t",
        headings_text=None,
        body=body,
        tags_text=None,
        metadata_json={"k": "v"},
        updated_at=datetime.now(UTC),
    )


def _ctx(tenant_id: UUID) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug="acme",
        actor_id="worker",
        actor_type=ActorType.SERVICE,
        roles=["service"],
        trace_id="trace-vec",
    )


# ---------------------------------------------------------------------------
# hash_embed
# ---------------------------------------------------------------------------


def test_hash_embed_is_deterministic() -> None:
    a = hash_embed("hello world")
    b = hash_embed("hello world")
    assert a == b
    assert len(a) == HASH_EMBED_DIM


def test_hash_embed_differs_by_input() -> None:
    assert hash_embed("alpha") != hash_embed("beta")


def test_hash_embed_is_unit_length() -> None:
    vec = hash_embed("some body")
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# QdrantVectorIndexWriter — behaviour verified via a fake client
# ---------------------------------------------------------------------------


class _FakeCollections:
    def __init__(self, names: list[str]) -> None:
        self.collections = [type("C", (), {"name": n})() for n in names]


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.created: list[tuple[str, Any]] = []
        self.upserts: list[tuple[str, list[Any]]] = []

    def get_collections(self) -> _FakeCollections:
        return _FakeCollections(sorted(self.collections))

    def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        self.collections.add(collection_name)
        self.created.append((collection_name, vectors_config))

    def upsert(self, *, collection_name: str, points: list[Any], wait: bool) -> None:  # noqa: ARG002
        self.upserts.append((collection_name, points))


def test_writer_creates_collection_and_writes_points() -> None:
    qdrant = pytest.importorskip("qdrant_client")  # noqa: F841
    tenant_id = uuid4()
    client = _FakeQdrantClient()
    writer = QdrantVectorIndexWriter(
        allow_test_embedding=True, client=client, embedding_dim=HASH_EMBED_DIM
    )

    writer.upsert(
        tenant_ctx=_ctx(tenant_id),
        tier=SearchTier.STABLE,
        chunks=[_upsert(tenant_id, "alpha"), _upsert(tenant_id, "beta")],
    )

    assert client.created, "collection should be created on first write"
    name = writer.collection_name(SearchTier.STABLE)
    from ragos.storage.vector import collection_name

    assert name == collection_name(
        embedding_profile="hash-test", embedding_version="sha256-hash-v1", tier=SearchTier.STABLE
    )
    assert client.created[0][0] == name
    assert len(client.upserts) == 1
    assert client.upserts[0][0] == name
    assert len(client.upserts[0][1]) == 2


def test_writer_idempotent_upsert_reuses_chunk_id() -> None:
    pytest.importorskip("qdrant_client")
    tenant_id = uuid4()
    client = _FakeQdrantClient()
    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=client)
    chunk_id = uuid4()

    chunk = _upsert(tenant_id, "alpha", chunk_id=chunk_id)
    writer.upsert(tenant_ctx=_ctx(tenant_id), tier=SearchTier.HOT, chunks=[chunk])
    writer.upsert(tenant_ctx=_ctx(tenant_id), tier=SearchTier.HOT, chunks=[chunk])

    assert len(client.upserts) == 2
    first_id = client.upserts[0][1][0].id
    second_id = client.upserts[1][1][0].id
    assert (
        first_id
        == second_id
        == str(
            uuid5(
                tenant_id,
                f"hot:enterprise:{chunk.kb_id}:{chunk.document_id}:{chunk.document_version_id}:{chunk.representation_id}:{chunk.index_id}:{chunk_id}",
            )
        )
    )


def test_writer_rejects_cross_tenant_chunk() -> None:
    pytest.importorskip("qdrant_client")
    tenant_ctx = _ctx(uuid4())
    other_tenant = uuid4()
    client = _FakeQdrantClient()
    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=client)

    with pytest.raises(PermissionDeniedError, match="tenant_id does not match"):
        writer.upsert(
            tenant_ctx=tenant_ctx,
            tier=SearchTier.STABLE,
            chunks=[_upsert(other_tenant, "x")],
        )


def test_writer_noop_on_empty_chunks() -> None:
    pytest.importorskip("qdrant_client")
    client = _FakeQdrantClient()
    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=client)

    writer.upsert(tenant_ctx=_ctx(uuid4()), tier=SearchTier.STABLE, chunks=[])

    assert client.upserts == []
    assert client.created == []


def test_writer_ensure_collection_is_cached() -> None:
    pytest.importorskip("qdrant_client")
    tenant_id = uuid4()
    client = _FakeQdrantClient()
    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=client)

    writer.upsert(
        tenant_ctx=_ctx(tenant_id),
        tier=SearchTier.STABLE,
        chunks=[_upsert(tenant_id, "a")],
    )
    writer.upsert(
        tenant_ctx=_ctx(tenant_id),
        tier=SearchTier.STABLE,
        chunks=[_upsert(tenant_id, "b")],
    )

    # collection existence check may run on first write, but create_collection
    # should only fire once.
    assert len(client.created) == 1


def test_chunk_payload_shape_contains_tenant_id_for_filter() -> None:
    tenant_id = uuid4()
    chunk = _upsert(tenant_id)

    payload = _chunk_payload(chunk)

    assert payload["tenant_id"] == str(tenant_id)
    assert payload["metadata"] == {"k": "v"}
    assert "layer" in payload


def test_writer_refuses_implicit_test_embedding_before_opening_client() -> None:
    with pytest.raises(ValueError, match="real embedding"):
        QdrantVectorIndexWriter(url="http://not-a-test-server.invalid")


def test_writer_refuses_existing_collection_with_a_different_dimension() -> None:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(":memory:")
    writer = QdrantVectorIndexWriter(client=client, allow_test_embedding=True)
    client.create_collection(
        collection_name=writer.collection_name(SearchTier.STABLE),
        vectors_config=VectorParams(size=3, distance=Distance.COSINE),
    )
    tenant_id = uuid4()
    with pytest.raises(ValueError, match="does not match"):
        writer.upsert(
            tenant_ctx=_ctx(tenant_id), tier=SearchTier.STABLE, chunks=[_upsert(tenant_id)]
        )
    client.close()
