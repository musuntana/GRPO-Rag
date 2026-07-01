from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

LOGGER = get_logger(__name__)

HASH_EMBED_DIM = 64

EmbeddingFn = Callable[[str], list[float]]


def hash_embed(text: str, *, dim: int = HASH_EMBED_DIM) -> list[float]:
    """Deterministic embedding stub — NOT semantic.

    Used only to exercise the write path, collection schema, and
    idempotent-upsert semantics. Real embedding providers will replace this
    at the ``embedding_fn`` injection point in ``QdrantVectorIndexWriter``.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    buf = (digest * ((dim // len(digest)) + 1))[:dim]
    raw = [(b / 255.0) - 0.5 for b in buf]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


def _chunk_payload(chunk: SearchChunkUpsert) -> dict[str, Any]:
    return {
        "tenant_id": str(chunk.tenant_id),
        "kb_id": str(chunk.kb_id) if chunk.kb_id else None,
        "document_id": str(chunk.document_id) if chunk.document_id else None,
        "parent_chunk_id": str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
        "layer": chunk.layer,
        "lang": chunk.lang,
        "title": chunk.title,
        "metadata": chunk.metadata_json,
    }


class QdrantVectorIndexWriter:
    """Writes chunk vectors to Qdrant with tenant isolation via payload filter.

    Collection layout: one collection per tier (e.g. ``ragos_stable``,
    ``ragos_hot``, ``ragos_memory``) — NOT per tenant. AGENTS.md forbids
    per-tenant collections; tenant isolation is enforced by a mandatory
    ``tenant_id`` payload filter on every read, and by refusing any upsert
    whose ``SearchChunkUpsert.tenant_id`` diverges from the active
    ``TenantContext``.

    Idempotency: point ids are the ``SearchChunkUpsert.id`` (the same UUID
    used in the FTS row), so repeated ingest of the same chunk overwrites
    the existing point.
    """

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        url: str | None = None,
        embedding_dim: int = HASH_EMBED_DIM,
        embedding_fn: EmbeddingFn | None = None,
        collection_prefix: str = "ragos",
    ) -> None:
        if client is None:
            if url is None:
                raise ValueError("QdrantVectorIndexWriter requires 'client' or 'url'")
            from qdrant_client import QdrantClient as _QdrantClient

            client = _QdrantClient(url=url)
        self.client = client
        self.embedding_dim = embedding_dim
        self.embedding_fn = embedding_fn or (lambda body: hash_embed(body, dim=embedding_dim))
        self.collection_prefix = collection_prefix
        self._ensured_collections: set[str] = set()

    def collection_name(self, tier: SearchTier) -> str:
        return f"{self.collection_prefix}_{tier.value}"

    def _ensure_collection(self, name: str) -> None:
        if name in self._ensured_collections:
            return
        from qdrant_client.models import Distance, VectorParams

        existing = {c.name for c in self.client.get_collections().collections}
        if name not in existing:
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=self.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            LOGGER.info("qdrant collection created", extra={"collection": name})
        self._ensured_collections.add(name)

    def upsert(
        self,
        *,
        tenant_ctx: TenantContext,
        tier: SearchTier,
        chunks: list[SearchChunkUpsert],
    ) -> None:
        if not chunks:
            return
        from qdrant_client.models import PointStruct

        collection = self.collection_name(tier)
        self._ensure_collection(collection)

        points = []
        for chunk in chunks:
            if chunk.tenant_id != tenant_ctx.tenant_id:
                raise ValueError(
                    "QdrantVectorIndexWriter: chunk tenant_id does not match tenant_ctx"
                )
            points.append(
                PointStruct(
                    id=str(chunk.id),
                    vector=self.embedding_fn(chunk.body),
                    payload=_chunk_payload(chunk),
                )
            )

        self.client.upsert(collection_name=collection, points=points, wait=True)
        LOGGER.info(
            "qdrant upsert complete",
            extra={
                "collection": collection,
                "count": len(points),
                "tenant_id": str(tenant_ctx.tenant_id),
            },
        )
