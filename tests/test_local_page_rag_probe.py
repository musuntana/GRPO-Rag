"""S1 synthetic page RAG probe: actual in-memory Qdrant, no paid providers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers import ChatChunk, EmbedResult
from ragos.retrieval.local_probe import DOC, KB, TENANT, _chunks, run_probe


@dataclass
class SyntheticProvider:
    answer: str = "The warranty lasts 24 months [1]."
    queries: int = 0
    chats: int = 0

    def embed(self, *, texts, model, parameters=None):
        assert model == "synthetic-qwen-embedding"
        vectors = []
        for text in texts:
            # These are fixed *test* vectors, not a real embedding model.
            if "warranty" in text.lower():
                vectors.append([1.0, 0.0, 0.0])
            elif "deposit" in text.lower():
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        self.queries += 1
        return EmbedResult(embeddings=vectors, model=model)

    def chat(self, *, messages, model, parameters=None):
        assert model == "synthetic-qwen-chat"
        assert "Contract section 7" in messages[1]["content"]
        self.chats += 1
        return ChatChunk(content=self.answer, model=model)


def _run(provider, *, client=None, version="test-synthetic-v1"):
    return run_probe(
        provider,
        embedding_model="synthetic-qwen-embedding",
        chat_model="synthetic-qwen-chat",
        embedding_version=version,
        client=client,
    )


def test_in_memory_qdrant_real_writer_and_citation_verifier() -> None:
    provider = SyntheticProvider()
    result = _run(provider)
    assert result.hit_at_2 == 1.0
    assert result.retrieved_pages[0] == 1
    assert result.structural_verdicts == ("ok",)
    assert result.semantic_verification == "not_checked"
    assert len(result.readback_digest) == 64
    assert provider.queries == 5
    assert provider.chats == 1


def test_unknown_citation_marker_is_never_verified() -> None:
    provider = SyntheticProvider(answer="The warranty lasts 24 months [99].")
    result = _run(provider)
    assert result.structural_verdicts == ("marker_out_of_range",)
    assert result.semantic_verification == "not_checked"


def test_probe_uses_scoped_qdrant_filter_and_no_persistent_pg_state() -> None:
    provider = SyntheticProvider()
    client = QdrantClient(":memory:")
    try:
        result = _run(provider, client=client)
        records, _ = client.scroll(collection_name=result.collection, limit=10)
        assert len(records) == 3
        assert {p.payload["tenant_id"] for p in records} == {str(TENANT)}
        assert {p.payload["kb_id"] for p in records} == {str(KB)}
        assert {p.payload["doc_id"] for p in records} == {str(DOC)}
        assert {p.payload["page_no"] for p in records} == {1, 2, 3}
        assert all("body" not in p.payload for p in records)
        assert {p.payload["page_count"] for p in records} == {3}
        assert all(p.payload["parent_chunk_id"] is not None for p in records)
    finally:
        client.close()


def test_missing_model_version_fails_before_vector_creation() -> None:
    with pytest.raises(ValidationError, match="explicit model"):
        _run(SyntheticProvider(), version="")


def test_query_embedding_dimension_change_fails_closed() -> None:
    class DriftingProvider(SyntheticProvider):
        def embed(self, *, texts, model, parameters=None):
            result = super().embed(texts=texts, model=model, parameters=parameters)
            if texts == ["How many months does the contract warranty last?"]:
                result.embeddings = [[0.0, 1.0]]
            return result

    with pytest.raises(ProviderError, match="dimension"):
        _run(DriftingProvider())


def test_real_page_chunker_produces_exact_source_spans() -> None:
    from ragos.retrieval.local_probe import PAGES

    chunks = _chunks()
    raw = "\n\n".join(text for _, text in PAGES)
    assert len(chunks) == 3
    assert [item.page_no for item in chunks] == [1, 2, 3]
    assert all(item.parent_unit == "page" and item.page_count == 3 for item in chunks)
    assert all(item.representation_char_count == len(raw) for item in chunks)
    assert all(raw[item.start_char_idx:item.end_char_idx] == item.body for item in chunks)
    assert len({item.parent_chunk_id for item in chunks}) == 3


def test_content_change_creates_distinct_immutable_source_and_chunk_ids() -> None:
    original = _chunks()
    changed = _chunks(
        page_texts=[
            "Contract section 7: warranty lasts 12 months instead.",
            "Contract section 8: the retained guarantee deposit is 5 percent.",
            "Contract section 9: invoice settlement requires a signed receipt.",
        ]
    )
    assert original[0].document_id == changed[0].document_id == DOC
    assert original[0].document_version_id != changed[0].document_version_id
    assert original[0].representation_id != changed[0].representation_id
    assert original[0].index_id != changed[0].index_id
    assert original[0].id != changed[0].id


def test_embedding_revision_changes_index_not_source() -> None:
    first = _chunks(embedding_version="qwen-embedding-v1")
    second = _chunks(embedding_version="qwen-embedding-v2")
    third = _chunks(embedding_version="qwen-embedding-v1")
    assert first[0].document_version_id == second[0].document_version_id
    assert first[0].representation_id == second[0].representation_id
    assert first[0].index_id != second[0].index_id
    assert [p.id for p in first] == [p.id for p in third]


def test_empty_pdf_page_must_not_be_silently_ignored() -> None:
    with pytest.raises(ValidationError, match="empty PDF page"):
        _chunks(page_texts=["contract", "", "invoice"])


def test_page_number_contiguity_comes_from_parser() -> None:
    from ragos.retrieval.local_probe import PAGES

    assert [c.page_no for c in _chunks()] == list(range(1, len(PAGES) + 1))
