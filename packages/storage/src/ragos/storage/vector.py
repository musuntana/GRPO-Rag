from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.storage.index_projection import ChunkProjection, projection_digest, projection_from_chunk
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
        "chunk_id": str(chunk.id),
        "doc_id": str(chunk.document_id) if chunk.document_id else None,
        "document_version_id": str(chunk.document_version_id)
        if chunk.document_version_id
        else None,
        "representation_id": str(chunk.representation_id) if chunk.representation_id else None,
        "index_id": str(chunk.index_id) if chunk.index_id else None,
        "representation_char_count": chunk.representation_char_count,
        "parent_unit": chunk.parent_unit,
        "parent_chunk_id": str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
        "layer": chunk.layer,
        "lang": chunk.lang,
        "title": chunk.title,
        "metadata": chunk.metadata_json,
        "page_no": chunk.page_no,
        "page_count": chunk.page_count,
        "start_char_idx": chunk.start_char_idx,
        "end_char_idx": chunk.end_char_idx,
        "element_type": chunk.element_type,
        "is_parent": chunk.is_parent,
        "brand_id": str(chunk.brand_id) if chunk.brand_id else None,
        "user_id": str(chunk.user_id) if chunk.user_id else None,
        "updated_at": chunk.updated_at.timestamp(),
    }


def collection_name(
    *, embedding_profile: str, embedding_version: str, tier: SearchTier, prefix: str = "ragos"
) -> str:
    """One physical namespace per immutable embedding space and tier."""
    from ragos.storage.artifacts import canonical_json

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", embedding_profile) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", prefix
    ):
        raise ValidationError("invalid embedding profile or collection prefix")
    if (
        not isinstance(embedding_version, str)
        or not embedding_version.strip()
        or len(embedding_version) > 128
    ):
        raise ValidationError("embedding version must be a frozen profile identifier")
    if not isinstance(tier, SearchTier):
        raise ValidationError("unknown physical vector tier")
    digest = hashlib.sha256(
        canonical_json(
            {"profile": embedding_profile, "version": embedding_version, "tier": tier.value}
        )
    ).hexdigest()
    return f"{prefix}_{embedding_profile}_{digest}_{tier.value}"


class QdrantVectorIndexWriter:
    """Writes chunk vectors to Qdrant with tenant isolation via payload filter.

    Collections are shared across tenants and separated by embedding profile,
    immutable model version and physical tier using the common naming function.
    Every read requires the tenant payload predicate; writes reject tenant,
    memory owner and knowledge-base authorization mismatches.

    Point ids namespace the immutable enterprise source chain; shadow indexes
    cannot overwrite published versions. Payload chunk_id retains the original UUID.
    Memory points use a separate owner namespace and no enterprise lineage.
    """

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        url: str | None = None,
        embedding_dim: int = HASH_EMBED_DIM,
        embedding_fn: EmbeddingFn | None = None,
        collection_prefix: str = "ragos",
        embedding_profile: str = "hash-test",
        allow_test_embedding: bool = False,
        embedding_version: str | None = None,
    ) -> None:
        if embedding_dim <= 0:
            raise ValueError("embedding dimension must be positive")
        if embedding_fn is None and not allow_test_embedding:
            raise ValueError(
                "real embedding function required; hash embedding needs explicit local test opt-in"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", collection_prefix):
            raise ValueError("invalid collection prefix")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", embedding_profile):
            raise ValueError("invalid embedding profile")
        if embedding_version is None:
            if embedding_fn is not None:
                raise ValidationError(
                    "embedding function requires an explicit immutable model version"
                )
            embedding_version = "sha256-hash-v1"
        if not embedding_version.strip() or len(embedding_version) > 128:
            raise ValidationError("embedding version must be a frozen profile identifier")
        self.embedding_version = embedding_version
        if client is None:
            if not url:
                raise ValueError("QdrantVectorIndexWriter requires 'client' or 'url'")
            from qdrant_client import QdrantClient as _QdrantClient

            client = _QdrantClient(url=url)
        self.client = client
        self.embedding_dim = embedding_dim
        self.embedding_profile = embedding_profile
        self.embedding_fn = embedding_fn or (lambda body: hash_embed(body, dim=embedding_dim))
        self.collection_prefix = collection_prefix
        self._ensured_collections: set[str] = set()

    def collection_name(self, tier: SearchTier) -> str:
        return collection_name(
            prefix=self.collection_prefix,
            embedding_profile=self.embedding_profile,
            embedding_version=self.embedding_version,
            tier=tier,
        )

    def vision_collection_name(self, profile: str) -> str:
        return f"{self.collection_prefix}_vision_{profile}"

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
        else:
            vectors = self.client.get_collection(name).config.params.vectors
            if (
                not isinstance(vectors, VectorParams)
                or vectors.size != self.embedding_dim
                or vectors.distance != Distance.COSINE
            ):
                raise ValueError("existing vector collection does not match embedding profile")
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

        self._validate_chunks(tenant_ctx=tenant_ctx, tier=tier, chunks=chunks)
        collection = self.collection_name(tier)
        projections = (
            {}
            if tier == SearchTier.MEMORY
            else {chunk.id: self._projection(chunk) for chunk in chunks}
        )

        points = []
        for chunk in chunks:
            vector = self.embedding_fn(chunk.body)
            self._validate_vector(vector)
            payload = _chunk_payload(chunk)
            if tier != SearchTier.MEMORY:
                payload.update(projections[chunk.id].model_dump(mode="json"))
            points.append(
                PointStruct(
                    id=self._point_id(tenant_ctx, tier, chunk), vector=vector, payload=payload
                )
            )

        self._ensure_collection(collection)
        self.client.upsert(collection_name=collection, points=points, wait=True)
        LOGGER.info(
            "qdrant upsert complete",
            extra={
                "collection": collection,
                "count": len(points),
                "tenant_id": str(tenant_ctx.tenant_id),
            },
        )

    def _validate_chunks(
        self, *, tenant_ctx: TenantContext, tier: SearchTier, chunks: list[SearchChunkUpsert]
    ) -> None:
        tenant_ctx.require_scope_key()
        for chunk in chunks:
            if tier == SearchTier.MEMORY:
                if any(
                    value is not None
                    for value in (
                        chunk.document_version_id,
                        chunk.representation_id,
                        chunk.index_id,
                        chunk.representation_char_count,
                        chunk.parent_unit,
                    )
                ):
                    raise PermissionDeniedError("memory must not carry enterprise source identity")
            elif any(
                not isinstance(value, UUID)
                for value in (
                    chunk.id,
                    chunk.tenant_id,
                    chunk.kb_id,
                    chunk.document_id,
                    chunk.document_version_id,
                    chunk.representation_id,
                    chunk.index_id,
                )
            ):
                raise PermissionDeniedError("enterprise vector requires complete source identity")
            if tier != SearchTier.MEMORY:
                if (
                    not isinstance(chunk.representation_char_count, int)
                    or isinstance(chunk.representation_char_count, bool)
                    or chunk.representation_char_count < 0
                    or chunk.parent_unit not in {"page", "token_window"}
                    or ((chunk.parent_unit == "page") != (chunk.page_count is not None))
                    or (chunk.page_count is not None and chunk.page_count < 1)
                ):
                    raise PermissionDeniedError(
                        "enterprise vector requires validated representation bounds"
                    )
            if chunk.tenant_id != tenant_ctx.tenant_id:
                raise PermissionDeniedError("chunk tenant_id does not match tenant_ctx")
            if tier == SearchTier.MEMORY and chunk.user_id != tenant_ctx.require_user_id():
                raise PermissionDeniedError("memory owner does not match")
            if (
                tier != SearchTier.MEMORY
                and tenant_ctx.allowed_kb_ids is not None
                and chunk.kb_id not in tenant_ctx.allowed_kb_ids
            ):
                raise PermissionDeniedError("knowledge base is not authorized")

    @staticmethod
    def _projection(chunk: SearchChunkUpsert) -> ChunkProjection:
        return projection_from_chunk(
            chunk,
            representation_char_count=chunk.representation_char_count,
            parent_unit=chunk.parent_unit,
        )

    def _validate_vector(self, vector) -> None:
        if (
            not isinstance(vector, list)
            or len(vector) != self.embedding_dim
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            )
        ):
            raise ValueError("embedding has wrong dimension or non-finite values")

    @staticmethod
    def _point_id(tenant_ctx: TenantContext, tier: SearchTier, chunk: SearchChunkUpsert) -> str:
        namespace = (
            f"{tier.value}:memory:{chunk.user_id}:{chunk.id}"
            if tier == SearchTier.MEMORY
            else f"{tier.value}:enterprise:{chunk.kb_id}:{chunk.document_id}:"
            f"{chunk.document_version_id}:{chunk.representation_id}:{chunk.index_id}:{chunk.id}"
        )
        return str(uuid5(tenant_ctx.tenant_id, namespace))

    def reconcile(
        self, *, tenant_ctx: TenantContext, tier: SearchTier, chunks: list[SearchChunkUpsert]
    ) -> str:
        """Read actual persisted points; no embedding or store mutation occurs."""
        if tier == SearchTier.MEMORY:
            raise PermissionDeniedError("memory cannot reconcile an enterprise index plan")
        self._validate_chunks(tenant_ctx=tenant_ctx, tier=tier, chunks=chunks)
        projections = [self._projection(chunk) for chunk in chunks]
        expected_digest = projection_digest(projections)
        if len({item.index_id for item in projections}) != 1:
            raise ValidationError("vector readback requires one frozen index")
        expected = {
            self._point_id(tenant_ctx, tier, chunk): projection.model_dump(mode="json")
            for chunk, projection in zip(chunks, projections, strict=True)
        }
        points = self.client.retrieve(
            collection_name=self.collection_name(tier),
            ids=list(expected),
            with_payload=True,
            with_vectors=True,
        )
        seen = set()
        actual = []
        for point in points:
            point_id = str(point.id)
            if point_id not in expected or point_id in seen:
                raise ValidationError("vector readback has duplicate or unexpected point identity")
            seen.add(point_id)
            payload = point.payload or {}
            if not set(ChunkProjection.model_fields).issubset(payload):
                raise ValidationError("vector readback is missing projection fields")
            selected = {key: payload.get(key) for key in ChunkProjection.model_fields}
            if selected != expected[point_id]:
                raise ValidationError("vector readback does not match frozen projection")
            self._validate_vector(point.vector)
            actual.append(ChunkProjection.model_validate(selected))
        if seen != set(expected):
            raise ValidationError("vector readback is missing planned points")
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        count_filter = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_ctx.tenant_id))),
                FieldCondition(
                    key="index_id", match=MatchValue(value=str(projections[0].index_id))
                ),
            ]
        )
        count = self.client.count(
            collection_name=self.collection_name(tier), count_filter=count_filter, exact=True
        ).count
        if count != len(expected):
            raise ValidationError("vector index contains unexpected points")
        digest = projection_digest(actual)
        if digest != expected_digest:
            raise ValidationError("vector readback digest differs from plan")
        return digest


@dataclass(frozen=True, slots=True)
class FilterClause:
    key: str
    value: Any
    operator: str = "match"


@dataclass(slots=True)
class FilterSpec:
    must: list[FilterClause] = field(default_factory=list)
    should: list[FilterClause] = field(default_factory=list)

    def keys(self) -> set[str]:
        return {clause.key for clause in self.must + self.should}


def build_filter_spec(
    scope_key: str,
    *,
    kb_ids=None,
    brand_ids=None,
    layers=None,
    element_types=None,
    is_parent=None,
    min_updated_at=None,
    user_id=None,
) -> FilterSpec:
    if not scope_key:
        raise PermissionDeniedError("unscoped filter: scope_key required")
    parts = scope_key.split("/") if scope_key else []
    if len(parts) != 2 or parts[0] != "tenant":
        raise PermissionDeniedError("malformed scope_key")
    try:
        tenant_id = UUID(parts[1])
    except ValueError as exc:
        raise PermissionDeniedError("invalid scope_key") from exc
    spec = FilterSpec(must=[FilterClause("tenant_id", str(tenant_id))])
    for key, values in (("kb_id", kb_ids), ("layer", layers), ("element_type", element_types)):
        if values is not None:
            spec.must.append(FilterClause(key, [str(v) for v in values], "any"))
    if user_id is not None:
        spec.must.append(FilterClause("user_id", str(user_id)))
    else:
        for key in (
            "document_version_id",
            "representation_id",
            "index_id",
            "doc_id",
            "kb_id",
            "chunk_id",
            "representation_char_count",
            "parent_unit",
        ):
            spec.must.append(FilterClause(key, None, "not_empty"))
    if is_parent is not None:
        spec.must.append(FilterClause("is_parent", is_parent))
    if min_updated_at is not None:
        spec.must.append(FilterClause("updated_at", min_updated_at, "gte"))
    if brand_ids is not None:
        spec.should = [
            FilterClause("brand_id", [str(v) for v in brand_ids], "any"),
            FilterClause("brand_id", None, "null"),
        ]
    return spec


def build_filter(scope_key: str, **kwargs):
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        IsEmptyCondition,
        IsNullCondition,
        MatchAny,
        MatchValue,
        PayloadField,
        Range,
    )

    spec = build_filter_spec(scope_key, **kwargs)

    def condition(clause):
        if clause.operator == "not_empty":
            return Filter(must_not=[IsEmptyCondition(is_empty=PayloadField(key=clause.key))])
        if clause.operator == "null":
            return IsNullCondition(is_null=PayloadField(key=clause.key))
        if clause.operator == "gte":
            return FieldCondition(key=clause.key, range=Range(gte=clause.value))
        if clause.operator == "any":
            return FieldCondition(key=clause.key, match=MatchAny(any=clause.value))
        return FieldCondition(key=clause.key, match=MatchValue(value=clause.value))

    return Filter(
        must=[condition(c) for c in spec.must], should=[condition(c) for c in spec.should] or None
    )
