"""Real PDF-byte extraction tests using a tiny PDF generated without libraries.

CI installs Poppler; no LLM network calls, external PDF files or credentials.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers import ChatChunk, EmbedResult
from ragos.retrieval.local_probe import DOC, _chunks, run_probe
from ragos.retrieval.parse.page_chunker import ChunkerConfig, PageChunker
from ragos.retrieval.parse.pdf_bytes import extract_pdf_bytes

POPPLER_INSTALLED = shutil.which("pdftotext") is not None
needs_poppler = pytest.mark.skipif(not POPPLER_INSTALLED, reason="poppler-utils is not installed")


def _make_pdf(pages: list[str]) -> bytes:
    """Build a valid, small, two-level PDF object tree with real text operators."""
    count = len(pages)
    font_id = 3 + 2 * count
    children = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{children}] /Count {count} >>".encode(),
    ]
    for i, text in enumerate(pages):
        page_id = 3 + 2 * i
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 50 740 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode()
            + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode())
        pdf.extend(value)
        pdf.extend(b"\nendobj\n")
    start_xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start_xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


@needs_poppler
def test_real_pdf_bytes_preserve_pages_and_text_offsets() -> None:
    pdf = _make_pdf(["The warranty lasts 24 months.", "The deposit is 5 percent."])
    document = extract_pdf_bytes(pdf, title="Test contract")
    assert document.page_count == 2
    assert document.title == "Test contract"
    assert [page.page_no for page in document.pages] == [1, 2]
    assert [page.text() for page in document.pages] == [
        "The warranty lasts 24 months.",
        "The deposit is 5 percent.",
    ]
    assert document.metadata["pdf_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert document.metadata["parser_mode"] == "text_only"
    assert document.parent_unit == "page"

    nodes = PageChunker(ChunkerConfig(parent_unit="page")).chunk(document)
    flat = document.flat_text()
    assert {node.page_no for node in nodes} == {1, 2}
    assert sum(node.is_parent for node in nodes) == 2
    assert all(flat[node.start_char_idx:node.end_char_idx] == node.text for node in nodes)
    assert all(node.page_count == 2 for node in nodes)


@needs_poppler
def test_real_pdf_bytes_feed_qdrant_and_structural_citation() -> None:
    class FixtureModels:
        def embed(self, *, texts, model, parameters=None):
            assert model == "test-embedding"
            vectors = [
                [1.0, 0.0, 0.0] if "warranty" in text.lower()
                else [0.0, 1.0, 0.0]
                for text in texts
            ]
            return EmbedResult(embeddings=vectors, model=model)

        def chat(self, *, messages, model, parameters=None):
            assert model == "test-chat"
            assert "warranty lasts 24 months" in messages[1]["content"]
            return ChatChunk(content="The warranty lasts 24 months [1].", model=model)

    pdf = _make_pdf([
        "Contract section 7: the warranty lasts 24 months.",
        "Contract section 8: the deposit is 5 percent.",
    ])
    result = run_probe(
        FixtureModels(),
        embedding_model="test-embedding",
        chat_model="test-chat",
        embedding_version="fixture-revision-1",
        pdf_bytes=pdf,
    )
    assert result.hit_at_2 == 1.0
    assert result.retrieved_pages[0] == 1
    assert result.structural_verdicts == ("ok",)
    assert result.semantic_verification == "not_checked"


@needs_poppler
def test_pdf_source_bytes_change_version_even_if_extracted_text_is_same() -> None:
    original = _make_pdf(["The warranty lasts 24 months."])
    changed_bytes = original + b"%non-semantic test metadata\n"
    before = _chunks(pdf_bytes=original)
    after = _chunks(pdf_bytes=changed_bytes)
    assert before[0].document_id == after[0].document_id == DOC
    assert before[0].document_version_id != after[0].document_version_id
    assert before[0].representation_id != after[0].representation_id


@needs_poppler
def test_scan_only_or_blank_page_fails_instead_of_shifting_page_ids() -> None:
    pdf = _make_pdf(["This page contains text.", ""])
    with pytest.raises(ValidationError, match="no extractable text"):
        extract_pdf_bytes(pdf)


@needs_poppler
def test_pdf_input_output_and_page_limits_are_enforced() -> None:
    pdf = _make_pdf(["The warranty lasts 24 months.", "Deposit information."])
    with pytest.raises(ValidationError, match="input limit"):
        extract_pdf_bytes(pdf, max_pdf_bytes=30)
    with pytest.raises(ValidationError, match="output limit"):
        extract_pdf_bytes(pdf, max_text_bytes=6)
    with pytest.raises(ValidationError, match="page limit"):
        extract_pdf_bytes(pdf, max_pages=1)


def test_non_pdf_is_refused_before_external_parser() -> None:
    with pytest.raises(ValidationError, match="not a valid PDF"):
        extract_pdf_bytes(b"not a PDF")


def test_missing_poppler_is_a_hard_error() -> None:
    pdf = _make_pdf(["Sample text."])
    with pytest.raises(ProviderError, match="not available"):
        extract_pdf_bytes(pdf, executable="ragos-poppler-not-present-test")


def test_invalid_resource_limits_are_refused() -> None:
    pdf = _make_pdf(["Sample text."])
    with pytest.raises(ValidationError, match="resource bounds"):
        extract_pdf_bytes(pdf, max_pages=0)
