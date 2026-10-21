"""Real synthetic-PDF input through Poppler, Qdrant and model contracts.

A deterministic synthetic provider stands in for a neural model in CI.
Only an operator running scripts/check_local_pdf_rag.py actually calls Qwen.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import ProviderError, ValidationError
from ragos.retrieval.local_probe import PAGES
from ragos.retrieval.parse.pdf_bytes import extract_pdf_bytes
from ragos.retrieval.synthetic_pdf import make_synthetic_pdf
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos.runtime.local_pdf_model_probe import run_synthetic_pdf_model_probe
from ragos.storage.repositories.search import SearchTier
from ragos.storage.vector import collection_name
from test_local_page_rag_probe import SyntheticProvider
from test_qdrant_server_pg_reconnect import qdrant_url

__all__ = ["qdrant_url"]

needs_poppler = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="Poppler is not installed"
)


def _spec(dimension=3):
    return FrozenEmbeddingSpec(
        model="synthetic-qwen-embedding", model_digest="a" * 12,
        dimension=dimension,
    )


@needs_poppler
def test_actual_generated_pdf_bytes_feed_rag_and_structural_verification() -> None:
    provider = SyntheticProvider()
    report = run_synthetic_pdf_model_probe(
        provider, embedding_spec=_spec(), chat_model="synthetic-qwen-chat"
    )
    assert report["source_kind"] == "generated_synthetic_pdf_bytes"
    assert report["source_page_count"] == 3
    assert report["retrieved_page_numbers"][0] == 1
    assert report["hit_at_2"] == 1.0
    assert report["structural_citation_verdicts"] == ["ok"]
    assert report["semantic_verification"] == "not_checked"
    assert report["model_weight_attestation"] == "not_verified"
    assert report["production_gateway_verification"] == "not_performed"
    assert report["embedding_dimension"] == 3
    assert provider.queries == 5
    assert provider.chats == 1
    assert "The warranty lasts" not in json.dumps(report)
    assert report["source_sha256"] == run_synthetic_pdf_model_probe(
        SyntheticProvider(), embedding_spec=_spec(), chat_model="synthetic-qwen-chat"
    )["source_sha256"]


def test_pdf_builder_has_real_page_tree_and_validates_synthetic_inputs() -> None:
    pdf = make_synthetic_pdf([p for _, p in PAGES])
    assert pdf.startswith(b"%PDF-")
    assert pdf.endswith(b"%%EOF\n")
    assert pdf == make_synthetic_pdf([p for _, p in PAGES])
    for invalid in [[], ["bad\nnewline"], ["emoji 🚀"], ["page"] * 11]:
        with pytest.raises(ValidationError, match="synthetic PDF"):
            make_synthetic_pdf(invalid)


@needs_poppler
def test_generated_pdf_roundtrips_through_real_poppler_pages() -> None:
    pdf = make_synthetic_pdf([p for _, p in PAGES])
    parsed = extract_pdf_bytes(pdf, title="synthetic test")
    assert parsed.page_count == 3
    assert [page.text() for page in parsed.pages] == [p for _, p in PAGES]


@needs_poppler
def test_wrong_frozen_embedding_dimension_refuses_before_chat() -> None:
    provider = SyntheticProvider()
    with pytest.raises(ProviderError, match="dimension"):
        run_synthetic_pdf_model_probe(
            provider, embedding_spec=_spec(2), chat_model="synthetic-qwen-chat"
        )
    assert provider.chats == 0


@needs_poppler
def test_nonfinite_and_all_zero_model_vectors_are_rejected() -> None:
    class InvalidEmbedding(SyntheticProvider):
        def __init__(self, *, mode):
            super().__init__()
            self.mode = mode

        def embed(self, *, texts, model, parameters=None):
            result = super().embed(texts=texts, model=model, parameters=parameters)
            result.embeddings = [
                [float("nan"), 0.0, 1.0]
                if self.mode == "nan" else [0.0, 0.0, 0.0]
            ]
            return result

    for mode in ("nan", "zero"):
        provider = InvalidEmbedding(mode=mode)
        with pytest.raises(ProviderError, match="numeric contract"):
            run_synthetic_pdf_model_probe(
                provider, embedding_spec=_spec(), chat_model="synthetic-qwen-chat"
            )
        assert provider.chats == 0


@needs_poppler
def test_optional_real_qdrant_http_server_uses_synthetic_pdf_only(qdrant_url) -> None:
    prefix = "ragos_real_pdf_ci_" + uuid4().hex[:12]
    spec = _spec()
    collection = collection_name(
        prefix=prefix, embedding_profile=spec.profile,
        embedding_version=spec.version, tier=SearchTier.STABLE,
    )
    client = QdrantClient(url=qdrant_url, timeout=5)
    try:
        try:
            report = run_synthetic_pdf_model_probe(
                SyntheticProvider(), embedding_spec=spec,
                chat_model="synthetic-qwen-chat",
                client=client, collection_prefix=prefix,
            )
            assert report["vector_store"] == "operator_provided_local_qdrant"
            assert report["hit_at_2"] == 1.0
            assert collection in {x.name for x in client.get_collections().collections}
            rows, _ = client.scroll(collection_name=collection, limit=10)
            assert {p.payload["page_no"] for p in rows} == {1, 2, 3}
            assert all("body" not in p.payload for p in rows)
        finally:
            if collection in {x.name for x in client.get_collections().collections}:
                client.delete_collection(collection_name=collection)
    finally:
        client.close()


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_local_pdf_rag.py"
    spec = importlib.util.spec_from_file_location("check_local_pdf_rag", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_rejects_remote_urls_and_missing_models(monkeypatch, capsys) -> None:
    cli = _load_cli()
    for url in (
        "https://api.example.com/v1",
        "http://10.0.0.1:12434/v1",
        "http://user:secret@localhost:12434/v1",
    ):
        with pytest.raises(ValidationError, match="local"):
            cli._check_local_url(url)
    with pytest.raises(ValidationError, match="local"):
        cli._check_local_url("http://example.com:6333", qdrant=True)

    for key in (
        "RAGOS_PROBE_BASE_URL", "RAGOS_PROBE_CHAT_MODEL",
        "RAGOS_PROBE_EMBED_MODEL", "RAGOS_PROBE_EMBED_REVISION",
        "RAGOS_PROBE_EMBED_DIM",
    ):
        monkeypatch.delenv(key, raising=False)
    assert cli.main([]) == 2
    assert "RAGOS_PROBE_" in capsys.readouterr().err
