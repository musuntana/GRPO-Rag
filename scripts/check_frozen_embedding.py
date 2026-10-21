"""Operator opt-in real Qwen Embedding smoke: synthetic pages, local Qdrant only.

Not a tenant gateway or a production document upload. This script never
reads a real customer PDF and never logs the API key, vector contents or source.
"""

from __future__ import annotations

import os
import sys

from qdrant_client import QdrantClient
from ragos.core.errors import RagOSError
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ragos.retrieval.local_probe import QUESTION, _chunks, _context
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec, LocalFrozenEmbedding
from ragos.storage.repositories.search import SearchTier
from ragos.storage.vector import build_filter


def main() -> int:
    base = os.getenv("RAGOS_PROBE_BASE_URL", "")
    model = os.getenv("RAGOS_PROBE_EMBED_MODEL", "")
    revision = os.getenv("RAGOS_PROBE_EMBED_REVISION", "")
    dimension = os.getenv("RAGOS_PROBE_EMBED_DIM", "")
    if not all([base, model, revision, dimension]):
        print(
            "Set RAGOS_PROBE_BASE_URL, RAGOS_PROBE_EMBED_MODEL, "
            "RAGOS_PROBE_EMBED_REVISION and RAGOS_PROBE_EMBED_DIM",
            file=sys.stderr,
        )
        return 2
    try:
        spec = FrozenEmbeddingSpec(
            model=model,
            model_digest=revision,
            dimension=int(dimension),
            profile=os.getenv("RAGOS_PROBE_EMBED_PROFILE", "qwen3_embedding"),
        )
        provider = OpenAICompatibleProvider(
            base_url=base, api_key=os.getenv("RAGOS_PROBE_API_KEY") or None
        )
        binding = LocalFrozenEmbedding(provider, spec, local_probe_opt_in=True)
        ctx = _context()
        chunks = _chunks(embedding_version=spec.version)
        with QdrantClient(":memory:") as client:
            writer = binding.writer(client, collection_prefix="ragos_local_qwen")
            writer.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=chunks)
            digest = writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=chunks)
            query = binding.embed_one(QUESTION)
            found = client.query_points(
                collection_name=writer.collection_name(SearchTier.STABLE),
                query=query,
                query_filter=build_filter(ctx.require_scope_key(), kb_ids=[chunks[0].kb_id]),
                limit=3,
                with_payload=True,
            )
            if not found.points:
                print("Embedding request succeeded but Qdrant returned zero pages")
                return 1
            print(
                f"OK model_revision={spec.model_digest} dimension={spec.dimension} "
                f"candidates={len(found.points)} projection_digest={digest}"
            )
            print("Test-only synthetic pages; model relevance and citations not evaluated.")
            return 0
    except (ValueError, RagOSError) as exc:
        print(f"Local embedding probe failed ({type(exc).__name__}); no source leaked")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
