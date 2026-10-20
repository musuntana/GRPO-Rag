"""Local-only RAG integration probe using synthetic paginated source evidence.

This runs the real Qdrant writer/readback, tenant payload filter, page-aware
retrieval metrics and structural citation verifier in an ephemeral collection.
It does NOT persist a document, authorize a user, serve production requests,
or claim semantically verified citations or a benchmark.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_DNS, UUID, uuid5

from qdrant_client import QdrantClient

from ragos.core.errors import ProviderError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ragos.retrieval.citation import EvidenceSet, bind_markers
from ragos.retrieval.gateway import build_generation_messages
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.metrics import Grade, hit_at_k
from ragos.retrieval.types import GoldEvidence, ScoredChunk
from ragos.retrieval.verify import verify_structural_batch
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter, build_filter

TENANT = uuid5(NAMESPACE_DNS, "ragos.s1.probe.tenant")
KB = uuid5(NAMESPACE_DNS, "ragos.s1.probe.kb")
DOC = uuid5(NAMESPACE_DNS, "ragos.s1.probe.contract")
DOC_VERSION = uuid5(NAMESPACE_DNS, "ragos.s1.probe.contract.version.1")
REPRESENTATION = uuid5(NAMESPACE_DNS, "ragos.s1.probe.representation.1")
INDEX = uuid5(NAMESPACE_DNS, "ragos.s1.probe.index.1")
PAGES = (
    (7, "Contract section 7: the warranty lasts 24 months after acceptance."),
    (8, "Contract section 8: the retained guarantee deposit is 5 percent."),
    (9, "Contract section 9: invoice settlement requires a signed receipt."),
)
QUESTION = "How many months does the contract warranty last?"
EMBEDDING_VERSION = "local-probe-only-explicit-model-revision"


class ProbeProvider(Protocol):
    def embed(self, *, texts: list[str], model: str, parameters=None): ...
    def chat(self, *, messages: list[dict], model: str, parameters=None): ...


@dataclass(frozen=True)
class ProbeResult:
    collection: str
    readback_digest: str
    retrieved_pages: tuple[int, ...]
    hit_at_2: float
    generated_answer: str
    structural_verdicts: tuple[str, ...]
    semantic_verification: str = "not_checked"


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT,
        tenant_slug="synthetic",
        actor_id="local-probe-only",
        trace_id="s1-page-rag-probe",
        allowed_kb_ids=[KB],
    )


def _chunks() -> list[SearchChunkUpsert]:
    updated = datetime(2026, 10, 20, tzinfo=UTC)
    return [
        SearchChunkUpsert(
            id=uuid5(NAMESPACE_DNS, f"ragos.s1.probe.page.{page}"),
            tenant_id=TENANT,
            kb_id=KB,
            document_id=DOC,
            document_version_id=DOC_VERSION,
            representation_id=REPRESENTATION,
            index_id=INDEX,
            parent_chunk_id=None,
            layer="L1",
            lang="en",
            title="Synthetic service contract",
            headings_text=None,
            body=text,
            tags_text=None,
            metadata_json={},
            updated_at=updated,
            page_no=page,
            page_count=9,
            start_char_idx=0,
            end_char_idx=len(text),
            page_offset_start=0,
            page_offset_end=len(text),
            representation_char_count=1000,
            parent_unit="page",
        )
        for page, text in PAGES
    ]


def _scored_chunk(chunk: SearchChunkUpsert, score: float) -> ScoredChunk:
    identity = EvidenceIdentity(
        tenant_id=TENANT,
        kb_id=KB,
        document_id=DOC,
        document_version_id=DOC_VERSION,
        representation_id=REPRESENTATION,
        index_id=INDEX,
        chunk_id=chunk.id,
        representation_char_count=chunk.representation_char_count,
        parent_unit="page",
        page_count=chunk.page_count,
    )
    return ScoredChunk(
        chunk_id=chunk.id,
        doc_id=chunk.document_id,
        kb_id=chunk.kb_id,
        text=chunk.body,
        source_text=chunk.body,
        score=score,
        channel="dense",
        page_no=chunk.page_no,
        page_count=chunk.page_count,
        start_char_idx=chunk.start_char_idx,
        end_char_idx=chunk.end_char_idx,
        source_identity=identity,
    )


def run_probe(
    provider: ProbeProvider,
    *,
    embedding_model: str,
    chat_model: str,
    embedding_version: str,
    client: QdrantClient | None = None,
) -> ProbeResult:
    """Execute the synthetic evidence path without durable tenant state."""
    if not embedding_model or not chat_model or not embedding_version:
        raise ValidationError("probe requires explicit model names and embedding version")
    if embedding_version == "hash-test":
        raise ValidationError("hash-test is not an actual embedding model revision")
    owns_client = client is None
    vector_client = client or QdrantClient(":memory:")
    try:
        ctx = _context()
        chunks = _chunks()

        def embed_one(text: str) -> list[float]:
            result = provider.embed(texts=[text], model=embedding_model)
            if len(result.embeddings) != 1:
                raise ProviderError("probe embedding provider returned wrong count")
            return result.embeddings[0]

        first_vector = embed_one(chunks[0].body)
        writer = QdrantVectorIndexWriter(
            client=vector_client,
            embedding_fn=embed_one,
            embedding_dim=len(first_vector),
            embedding_profile="s1_probe",
            embedding_version=embedding_version,
            collection_prefix="ragos_local",
        )
        writer.upsert(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=chunks)
        digest = writer.reconcile(tenant_ctx=ctx, tier=SearchTier.STABLE, chunks=chunks)
        query_vector = embed_one(QUESTION)
        if len(query_vector) != len(first_vector):
            raise ProviderError("query embedding dimension differs from frozen collection")

        response = vector_client.query_points(
            collection_name=writer.collection_name(SearchTier.STABLE),
            query=query_vector,
            query_filter=build_filter(ctx.require_scope_key(), kb_ids=[KB]),
            limit=2,
            with_payload=True,
        )
        by_id = {chunk.id: chunk for chunk in chunks}
        evidence = []
        for point in response.points:
            payload = point.payload or {}
            try:
                chunk_id = UUID(payload["chunk_id"])
            except (KeyError, TypeError, ValueError):
                raise ValidationError("probe vector result lacks chunk identity") from None
            chunk = by_id.get(chunk_id)
            if (
                chunk is None
                or payload.get("tenant_id") != str(TENANT)
                or payload.get("kb_id") != str(KB)
                or payload.get("doc_id") != str(DOC)
                or payload.get("document_version_id") != str(DOC_VERSION)
                or payload.get("representation_id") != str(REPRESENTATION)
                or payload.get("index_id") != str(INDEX)
                or payload.get("page_no") != chunk.page_no
            ):
                raise ValidationError("probe vector result does not match frozen evidence")
            evidence.append(_scored_chunk(chunk, float(point.score)))
        if not evidence:
            raise ValidationError("probe query returned no authorized evidence")

        gold = [GoldEvidence(doc_id=DOC, grade=3, page_no=7)]
        grades = [Grade(str(x.doc_id), grade=0, page_no=x.page_no) for x in evidence]
        hit = hit_at_k(grades, gold, 2)
        answer = provider.chat(
            messages=build_generation_messages(QUESTION, evidence),
            model=chat_model,
        ).content
        citations = bind_markers(answer, EvidenceSet(chunks=tuple(evidence)))
        verdicts = verify_structural_batch(citations, EvidenceSet(chunks=tuple(evidence)))
        return ProbeResult(
            collection=writer.collection_name(SearchTier.STABLE),
            readback_digest=digest,
            retrieved_pages=tuple(x.page_no for x in evidence),
            hit_at_2=hit,
            generated_answer=answer,
            structural_verdicts=tuple(v.verdict.value for v in verdicts),
        )
    finally:
        if owns_client:
            vector_client.close()


def main() -> int:
    base_url = os.environ.get("RAGOS_PROBE_BASE_URL")
    chat_model = os.environ.get("RAGOS_PROBE_CHAT_MODEL")
    embed_model = os.environ.get("RAGOS_PROBE_EMBED_MODEL")
    revision = os.environ.get("RAGOS_PROBE_EMBED_REVISION")
    if not all((base_url, chat_model, embed_model, revision)):
        print("Set RAGOS_PROBE_BASE_URL, CHAT_MODEL, EMBED_MODEL and EMBED_REVISION")
        return 2
    from ragos.core.errors import RagOSError

    try:
        result = run_probe(
            OpenAICompatibleProvider(
                base_url=base_url, api_key=os.environ.get("RAGOS_PROBE_API_KEY") or None
            ),
            embedding_model=embed_model,
            chat_model=chat_model,
            embedding_version=revision,
        )
    except RagOSError as exc:
        print(f"Probe failed: {exc.code} (details suppressed)")
        return 1
    print(
        f"page_recall_probe hit@2={result.hit_at_2} "
        f"pages={result.retrieved_pages} digest={result.readback_digest}"
    )
    print(f"citations structural={result.structural_verdicts}")
    print("semantic_verification=not_checked")
    # Answer text is intentionally not printed; source content is synthetic.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
