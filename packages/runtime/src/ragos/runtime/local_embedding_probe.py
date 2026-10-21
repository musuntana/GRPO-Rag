"""Explicit LOCAL-ONLY frozen Embedding bridge for S1 ingestion/recall probes.

This is not the tenant ModelRouter: credentials, metering, entitlements and
network egress must be enforced by a real gateway before production activation.
Only use synthetic/de-identified data with an operator-owned local endpoint.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from ragos.core.errors import ProviderError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.recall.dense import DenseRecall
from ragos.storage.repositories.search import SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter, collection_name


class EmbeddingTransport(Protocol):
    def embed(self, *, texts: list[str], model: str, parameters=None): ...


@dataclass(frozen=True, slots=True)
class FrozenEmbeddingSpec:
    """Model ID plus externally verified immutable digest and vector dimension.

    The operator must obtain `model_digest` from the installed model or weights;
    we do NOT pretend that calling /embeddings proves the weights' digest.
    A mutable model alias or a revision like 'latest' is not valid evidence.
    """

    model: str
    model_digest: str
    dimension: int
    profile: str = "qwen3_embedding"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 200
            or any(ch.isspace() for ch in self.model)
        ):
            raise ValidationError("local embedding requires an explicit installed model ID")
        if not re.fullmatch(r"[0-9a-f]{12,64}", self.model_digest):
            raise ValidationError("embedding revision requires an immutable 12-64 hex digest")
        if type(self.dimension) is not int or not 1 <= self.dimension <= 8192:
            raise ValidationError("embedding requires a frozen vector dimension")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.profile):
            raise ValidationError("invalid local embedding profile")

    @property
    def version(self) -> str:
        # Include the exact model request name as a fingerprint so two model
        # aliases with the same short revision cannot share a Qdrant namespace.
        name_digest = hashlib.sha256(self.model.encode("utf-8")).hexdigest()[:16]
        # Vector dimensions are part of the physical embedding space.
        # Never reuse a collection when the API changes vector size.
        return f"{name_digest}_{self.model_digest}_{self.dimension}"

    def collection(self, *, tier: SearchTier = SearchTier.STABLE) -> str:
        return collection_name(
            embedding_profile=self.profile, embedding_version=self.version, tier=tier
        )


class LocalFrozenEmbedding:
    """One source for index writer and DenseRecall query vectors in local tests."""

    def __init__(
        self,
        transport: EmbeddingTransport,
        spec: FrozenEmbeddingSpec,
        *,
        local_probe_opt_in: bool = False,
    ) -> None:
        if not local_probe_opt_in:
            raise ValidationError("direct provider use is permitted only in explicit local probes")
        self.transport = transport
        self.spec = spec

    def embed_one(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("embedding input must be nonempty text")
        try:
            response = self.transport.embed(texts=[text], model=self.spec.model)
            vectors = response.embeddings
        except (ValidationError, ProviderError):
            raise
        except Exception:
            raise ProviderError("local embedding model request failed") from None
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise ProviderError("embedding provider returned an invalid vector count")
        vector = vectors[0]
        if (
            not isinstance(vector, list)
            or len(vector) != self.spec.dimension
            or any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                for x in vector
            )
            or not any(x != 0 for x in vector)
        ):
            raise ProviderError("embedding differs from the pinned dimension or vector contract")
        return [float(x) for x in vector]

    def require_plan(self, plan) -> None:
        if (
            plan.embedding_profile != self.spec.profile
            or plan.embedding_version != self.spec.version
        ):
            raise ValidationError("index embedding identity differs from pinned model")

    def writer(self, client, *, collection_prefix: str = "ragos") -> QdrantVectorIndexWriter:
        return QdrantVectorIndexWriter(
            client=client,
            embedding_dim=self.spec.dimension,
            embedding_fn=self.embed_one,
            embedding_profile=self.spec.profile,
            embedding_version=self.spec.version,
            collection_prefix=collection_prefix,
        )

    async def query(self, text: str, tenant_ctx: TenantContext) -> list[float]:
        tenant_ctx.require_scope_key()
        return self.embed_one(text)

    def dense(self, client, *, source, plan, collection_prefix: str = "ragos") -> DenseRecall:
        """Refuse a different index profile before querying any vector store."""
        self.require_plan(plan)
        collection = collection_name(
            prefix=collection_prefix,
            embedding_profile=self.spec.profile,
            embedding_version=self.spec.version,
            tier=SearchTier.STABLE,
        )
        return DenseRecall(
            client,
            collection,
            self.query,
            source=source,
            embedding_profile=self.spec.profile,
            embedding_version=self.spec.version,
        )
