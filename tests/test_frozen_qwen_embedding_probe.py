"""Frozen Qwen-compatible embeddings across real PG and Qdrant (mock model HTTP).

GitHub CI validates the actual transport *schema*, frozen model/index identity,
real PDF -> PostgreSQL publication and Qdrant server readback. It does not
download Qwen or imply a real neural model was called.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec, LocalFrozenEmbedding
from ragos.storage.repositories.search import SearchRepository, SearchTier
from test_pdf_pg_index_integration import _real_pdf_fixture
from test_qdrant_server_pg_reconnect import qdrant_url
from test_storage_integration import database, scoped_session

__all__ = ["database", "qdrant_url"]

MODEL = "ragos/qwen3-embedding:0.6b-q8"
DIGEST = "79898cf0ff54"  # Only a synthetic test identifier, not a verified model.


class FixtureEmbeddingHTTP:
    def __init__(self):
        self.calls = []
        self.fail_dimension = False
        self.zero = False

    def __call__(self, url, payload, headers, timeout):
        assert url.endswith("/embeddings")
        assert payload["model"] == MODEL
        assert payload["encoding_format"] == "float"
        assert not headers.get("Authorization")
        assert timeout > 0
        texts = payload["input"]
        self.calls.extend(texts)
        vectors = []
        for text in texts:
            if self.zero:
                vectors.append([0.0, 0.0, 0.0])
            elif "warranty" in text.lower():
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
            if self.fail_dimension:
                vectors[-1] = [1.0, 0.0]
        return {"data": [{"index": i, "embedding": vec} for i, vec in enumerate(vectors)]}


def _binding(*, http=None, spec=None):
    http = http or FixtureEmbeddingHTTP()
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:12434/engines/v1",
        transport=http,
    )
    return LocalFrozenEmbedding(
        provider,
        spec or FrozenEmbeddingSpec(model=MODEL, model_digest=DIGEST, dimension=3),
        local_probe_opt_in=True,
    )


def test_frozen_binding_requires_real_revision_and_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        FrozenEmbeddingSpec(model=MODEL, model_digest="latest", dimension=3)
    with pytest.raises(ValidationError, match="dimension"):
        FrozenEmbeddingSpec(model=MODEL, model_digest=DIGEST, dimension=0)
    with pytest.raises(ValidationError, match="installed model"):
        FrozenEmbeddingSpec(model=" ", model_digest=DIGEST, dimension=3)
    provider = OpenAICompatibleProvider(base_url="http://localhost:12434/engines/v1")
    with pytest.raises(ValidationError, match="explicit local probes"):
        LocalFrozenEmbedding(
            provider, FrozenEmbeddingSpec(model=MODEL, model_digest=DIGEST, dimension=3)
        )


def test_index_namespace_changes_with_real_model_identity() -> None:
    before = FrozenEmbeddingSpec(model=MODEL, model_digest=DIGEST, dimension=3)
    changed_model = replace(before, model="other/embedding:0.6b")
    changed_revision = replace(before, model_digest="a" * 12)
    assert before.version != changed_model.version
    assert before.collection() != changed_model.collection()
    assert before.collection() != changed_revision.collection()
    changed_dimension = replace(before, dimension=2)
    assert before.version != changed_dimension.version
    assert before.collection() != changed_dimension.collection()
    assert before.version == replace(before).version


@pytest.mark.parametrize("mode", ["dimension", "zero"])
def test_bad_embedding_output_fails_before_qdrant_write(mode) -> None:
    http = FixtureEmbeddingHTTP()
    http.fail_dimension = mode == "dimension"
    http.zero = mode == "zero"
    binding = _binding(http=http)
    with pytest.raises(ProviderError, match="pinned dimension or vector"):
        binding.embed_one("test warranty")


def test_model_profile_mismatch_refuses_before_query() -> None:
    binding = _binding()
    class DifferentPlan:
        embedding_profile = "hash-test"
        embedding_version = "sha256-hash-v1"

    with pytest.raises(ValidationError, match="index embedding identity"):
        binding.dense(object(), source=object(), plan=DifferentPlan())


def test_qwen_compatible_embedding_protocol_indexes_and_recalls_pdf_pages(
    database, qdrant_url, tmp_path
):
    http = FixtureEmbeddingHTTP()
    binding = _binding(http=http)
    fixture = _real_pdf_fixture(database, tmp_path, embedding=binding)
    fixture.client.close()
    writer_client = QdrantClient(url=qdrant_url, timeout=5)
    collection_prefix = "ragos_qwen_ci_" + uuid4().hex[:12]
    fixture.client = writer_client
    fixture.writer = binding.writer(writer_client, collection_prefix=collection_prefix)
    collection = fixture.writer.collection_name(SearchTier.STABLE)
    reader_client = None
    try:
        binding.require_plan(fixture.plan)
        result = fixture.service().execute(manifest_id=fixture.plan.index_id)
        assert result.indexing_state == "indexed"
        assert fixture.writer.reconcile(
            tenant_ctx=fixture.ctx,
            tier=SearchTier.STABLE,
            chunks=fixture.plan.as_upserts(fixture.ctx),
        ) == fixture.plan.projection_digest(fixture.ctx)

        writer_client.close()
        reader_client = QdrantClient(url=qdrant_url, timeout=5)
        with scoped_session(database, fixture.ctx) as session:
            dense = binding.dense(
                reader_client, source=SearchRepository(session, fixture.ctx),
                plan=fixture.plan, collection_prefix=collection_prefix,
            )
            hits = asyncio.run(
                dense.recall(
                    "Contract warranty period is 24 months.",
                    fixture.ctx,
                    profile=_retriever_profile(fixture.ctx.tenant_id),
                )
            )
        assert len(hits) == len(fixture.plan.chunks) == 4
        assert {hit.page_no for hit in hits} == {1, 2}
        assert {hit.text for hit in hits} == {
            "Contract warranty period is 24 months.",
            "Contract retained deposit is 5 percent.",
        }
        assert all(hit.source_identity.index_id == fixture.plan.index_id for hit in hits)
        assert any("warranty" in value.lower() for value in http.calls)

        # Query requires the same frozen model/digest/dimension as ingestion.
        other = _binding(spec=replace(binding.spec, model_digest="a" * 12))
        with pytest.raises(ValidationError, match="index embedding identity"):
            other.dense(
                reader_client, source=object(), plan=fixture.plan,
                collection_prefix=collection_prefix,
            )
    finally:
        cleanup = reader_client or QdrantClient(url=qdrant_url, timeout=5)
        try:
            if collection in {c.name for c in cleanup.get_collections().collections}:
                cleanup.delete_collection(collection_name=collection)
        finally:
            cleanup.close()
            writer_client.close()


def _retriever_profile(tenant_id):
    from ragos.retrieval.types import RetrieverProfile

    return RetrieverProfile(
        id=uuid4(), tenant_id=tenant_id,
        recall_channels=["dense"], channel_budgets={"dense": 1.0},
        top_k_recall=10,
    )
