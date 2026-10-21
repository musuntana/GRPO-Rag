"""Local-only RAG integration module using synthetic paginated source evidence.

This runs the real Qdrant writer/readback, tenant payload filter, page-aware
retrieval metrics and structural citation verifier in an ephemeral collection.
It does NOT persist a document, authorize a user, serve production requests,
or claim semantically verified citations or a benchmark.
"""

from __future__ import annotations

import hashlib
import json
import math
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
from ragos.retrieval.parse.layout import LayoutElement, parse_pdf_pages
from ragos.retrieval.parse.page_chunker import ChunkerConfig, PageChunker
from ragos.retrieval.parse.pdf_bytes import extract_pdf_bytes
from ragos.retrieval.types import GoldEvidence, ScoredChunk
from ragos.retrieval.verify import verify_structural_batch
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter, build_filter

TENANT = uuid5(NAMESPACE_DNS, "ragos.s1.probe.tenant")
KB = uuid5(NAMESPACE_DNS, "ragos.s1.probe.kb")
DOC = uuid5(NAMESPACE_DNS, "ragos.s1.probe.contract")
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


def _chunks(
    *,
    embedding_version: str = EMBEDDING_VERSION,
    page_texts: list[str] | None = None,
    pdf_bytes: bytes | None = None,
) -> list[SearchChunkUpsert]:
    """Prepare real page-chunker output from already-extracted synthetic PDF text.

    Version, parser/chunker representation, index identity and child IDs derive
    from immutable source content. A changed page or embedding revision must
    never overwrite a previous point under the same source identity.
    """
    if not embedding_version.strip() or embedding_version == "hash-test":
        raise ValidationError("probe requires a frozen non-test embedding version")
    if pdf_bytes is not None and page_texts is not None:
        raise ValidationError("choose either PDF bytes or already extracted page text")
    if pdf_bytes is not None:
        parsed = extract_pdf_bytes(pdf_bytes, title="Synthetic PDF integration fixture")
    else:
        texts = [text for _, text in PAGES] if page_texts is None else page_texts
        parsed = parse_pdf_pages(
            [[LayoutElement(element_type="text", text=text, order=0)] for text in texts],
            parser_version="synthetic-pdf-elements-v1",
            title="Synthetic service contract",
        )
    if not parsed.pages or any(page.is_empty() for page in parsed.pages):
        raise ValidationError("cannot index an empty PDF page in the local probe")
    chunker = PageChunker(
        ChunkerConfig(
            child_size=400, parent_size=1600, parent_unit="page",
            parser_version=parsed.parser_version,
        )
    )
    nodes = chunker.chunk(parsed)
    body = parsed.flat_text()
    source_bytes = json.dumps(
        {
            "parser_version": parsed.parser_version,
            "page_texts": [page.text() for page in parsed.pages],
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    source_digest = (
        parsed.metadata["pdf_sha256"]
        if pdf_bytes is not None
        else hashlib.sha256(source_bytes).hexdigest()
    )
    doc_version = uuid5(DOC, f"source:{source_digest}")
    extracted_text_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    representation = uuid5(
        doc_version,
        f"parser:{parsed.parser_version};chunker:{chunker.config.version_id()};"
        f"extracted_text:{extracted_text_digest}",
    )
    index = uuid5(representation, f"embedding:{embedding_version}")
    node_ids = {
        node.chunk_id: uuid5(
            index,
            f"node:{node.order}:{node.page_no}:{node.is_parent}:"
            f"{node.start_char_idx}:{node.end_char_idx}:"
            f"{hashlib.sha256(node.text.encode('utf-8')).hexdigest()}",
        )
        for node in nodes
    }
    updated = datetime(2026, 10, 20, tzinfo=UTC)
    chunks = []
    for node in nodes:
        if node.is_parent:
            continue
        if (
            node.page_no is None or node.page_count != parsed.page_count
            or node.parent_chunk_id not in node_ids
            or node.start_char_idx is None or node.end_char_idx is None
            or node.page_offset_start is None or node.page_offset_end is None
            or body[node.start_char_idx:node.end_char_idx] != node.text
            or parsed.pages[node.page_no - 1].text()[
                node.page_offset_start:node.page_offset_end
            ] != node.text
        ):
            raise ValidationError("page chunk does not match its frozen source span")
        chunks.append(
            SearchChunkUpsert(
                id=node_ids[node.chunk_id],
                tenant_id=TENANT,
                kb_id=KB,
                document_id=DOC,
                document_version_id=doc_version,
                representation_id=representation,
                index_id=index,
                parent_chunk_id=node_ids[node.parent_chunk_id],
                layer="L1",
                lang="en",
                title=parsed.title,
                headings_text=None,
                body=node.text,
                tags_text=None,
                metadata_json={},
                updated_at=updated,
                page_no=node.page_no,
                page_count=node.page_count,
                start_char_idx=node.start_char_idx,
                end_char_idx=node.end_char_idx,
                page_offset_start=node.page_offset_start,
                page_offset_end=node.page_offset_end,
                representation_char_count=len(body),
                parent_unit="page",
                element_type=node.element_type,
            )
        )
    if not chunks:
        raise ValidationError("page chunker produced no indexable source evidence")
    return chunks


def _scored_chunk(chunk: SearchChunkUpsert, score: float) -> ScoredChunk:
    identity = EvidenceIdentity(
        tenant_id=TENANT,
        kb_id=KB,
        document_id=DOC,
        document_version_id=chunk.document_version_id,
        representation_id=chunk.representation_id,
        index_id=chunk.index_id,
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
    pdf_bytes: bytes | None = None,
    expected_embedding_dim: int | None = None,
    embedding_profile: str = "s1_probe",
    collection_prefix: str = "ragos_local",
) -> ProbeResult:
    """Execute the synthetic evidence path without durable tenant state."""
    if (
        expected_embedding_dim is not None
        and (
            type(expected_embedding_dim) is not int
            or not 1 <= expected_embedding_dim <= 8192
        )
    ):
        raise ValidationError("probe expected embedding dimension is invalid")
    if not embedding_model or not chat_model or not embedding_version:
        raise ValidationError("probe requires explicit model names and embedding version")
    if embedding_version == "hash-test":
        raise ValidationError("hash-test is not an actual embedding model revision")
    owns_client = client is None
    vector_client = client or QdrantClient(":memory:")
    try:
        ctx = _context()
        chunks = _chunks(embedding_version=embedding_version, pdf_bytes=pdf_bytes)

        def embed_one(text: str) -> list[float]:
            result = provider.embed(texts=[text], model=embedding_model)
            if not isinstance(result.embeddings, list) or len(result.embeddings) != 1:
                raise ProviderError("probe embedding provider returned wrong count")
            vector = result.embeddings[0]
            if (
                not isinstance(vector, list)
                or not vector
                or (
                    expected_embedding_dim is not None
                    and len(vector) != expected_embedding_dim
                )
                or any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in vector
                )
                or not any(value != 0 for value in vector)
            ):
                raise ProviderError("probe embedding dimension or numeric contract failed")
            return [float(value) for value in vector]

        first_vector = embed_one(chunks[0].body)
        writer = QdrantVectorIndexWriter(
            client=vector_client,
            embedding_fn=embed_one,
            embedding_dim=len(first_vector),
            embedding_profile=embedding_profile,
            embedding_version=embedding_version,
            collection_prefix=collection_prefix,
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
                or payload.get("document_version_id") != str(chunk.document_version_id)
                or payload.get("representation_id") != str(chunk.representation_id)
                or payload.get("index_id") != str(chunk.index_id)
                or payload.get("page_no") != chunk.page_no
            ):
                raise ValidationError("probe vector result does not match frozen evidence")
            evidence.append(_scored_chunk(chunk, float(point.score)))
        if not evidence:
            raise ValidationError("probe query returned no authorized evidence")

        gold = [GoldEvidence(doc_id=DOC, grade=3, page_no=1)]
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
