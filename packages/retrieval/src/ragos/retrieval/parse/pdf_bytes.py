"""Bounded, text-only Poppler PDF extraction into the canonical page contract.

Ingest adapter only: no OCR, page images, bounding boxes or production worker
activation. PDF bytes are untrusted; this process must run inside an isolated,
resource-limited ingest worker before accepting customer documents.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from ragos.core.errors import ProviderError, ValidationError
from ragos.retrieval.parse.layout import LayoutElement, ParsedDocument, parse_pdf_pages, validate

PARSER_VERSION = "poppler-pdftotext-layout-v1"
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PAGES = 200
TIMEOUT_SECONDS = 20.0


def extract_pdf_bytes(
    pdf_bytes: bytes,
    *,
    title: str | None = None,
    executable: str = "pdftotext",
    max_pdf_bytes: int = MAX_PDF_BYTES,
    max_text_bytes: int = MAX_TEXT_BYTES,
    max_pages: int = MAX_PAGES,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> ParsedDocument:
    """Extract genuine PDF file bytes, keeping every 1-based page boundary.

    The exact Poppler `-layout` plain-text representation is frozen into the
    parser version; a separate layout/vision parser is needed for table cells,
    bounding boxes, scans or images. Blank/scan-only pages are refused rather
    than silently renumbered or indexed as if they contained evidence.

    `executable` is a trusted operator dependency, not a client-supplied path.
    There is no shell, HTTP, password bypass, or model call.
    """
    if (
        type(max_pdf_bytes) is not int or max_pdf_bytes < 1
        or type(max_text_bytes) is not int or max_text_bytes < 1
        or type(max_pages) is not int or max_pages < 1
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 120
    ):
        raise ValidationError("PDF parser requires positive resource bounds")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValidationError("PDF source is not a valid PDF byte stream")
    if len(pdf_bytes) > max_pdf_bytes:
        raise ValidationError("PDF source exceeds configured input limit")
    binary = shutil.which(executable)
    if binary is None:
        raise ProviderError("Poppler PDF extractor is not available")

    with TemporaryDirectory(prefix="ragos-pdf-") as temp:
        source = Path(temp) / "source.pdf"
        text_file = Path(temp) / "extracted.txt"
        source.write_bytes(pdf_bytes)
        try:
            outcome = subprocess.run(
                [
                    binary, "-layout", "-enc", "UTF-8", "-eol", "unix",
                    str(source), str(text_file),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError("PDF extraction exceeded its deadline") from None
        except OSError:
            raise ProviderError("PDF extraction could not be executed") from None
        if outcome.returncode != 0 or not text_file.is_file():
            # Never include private PDF content, temp paths or Poppler stderr.
            raise ValidationError("PDF extraction failed or source is protected")
        if text_file.stat().st_size > max_text_bytes:
            raise ValidationError("PDF extracted text exceeds configured output limit")
        raw = text_file.read_bytes()

    # Poppler inserts a form-feed between pages and after the final page.
    # Removing only the final delimiter preserves blank interior pages for
    # rejection without shifting page numbers.
    if not raw or not raw.rstrip(b"\r\n").endswith(b"\x0c"):
        raise ValidationError("PDF extraction did not preserve page boundaries")
    parts = raw.rstrip(b"\r\n")[:-1].split(b"\x0c")
    if len(parts) > max_pages:
        raise ValidationError("PDF exceeds configured page limit")

    page_elements: list[list[LayoutElement]] = []
    for page_no, part in enumerate(parts, 1):
        try:
            page_text = part.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise ValidationError("PDF extraction returned invalid UTF-8") from None
        if not page_text:
            raise ValidationError(
                "PDF page has no extractable text; OCR or page review required",
                page_no=page_no,
            )
        page_elements.append([LayoutElement("text", page_text, 0)])

    document = parse_pdf_pages(
        page_elements,
        parser_version=PARSER_VERSION,
        title=title,
    )
    document.metadata["pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    document.metadata["text_sha256"] = hashlib.sha256(
        document.flat_text().encode("utf-8")
    ).hexdigest()
    document.metadata["parser_mode"] = "text_only"
    validate(document)
    return document
