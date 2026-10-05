"""Document parsing with element extraction.

Architecture §7.4 pipeline: ``Parser (layout analysis: text, tables, images)``.
With page anchoring (ADR-0006) this stage became load-bearing for correctness
rather than cosmetic — a parser regression now changes retrieval results, where
before it only changed chunk text.

Tables are linearized to text with the header row bound to every body row. That
makes a table retrievable through the ordinary text channel with no
table-specific embedding model or collection, and it makes a split table
detectable rather than silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ragos.core.errors import ValidationError


class SourceType(StrEnum):
    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    MARKDOWN = "markdown"
    HTML = "html"
    PLAIN = "plain"


#: Sources whose semantic unit is the page.
PAGINATED_SOURCES = frozenset({SourceType.PDF, SourceType.PPTX, SourceType.DOCX})


@dataclass(slots=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass(slots=True)
class LayoutElement:
    """One atomic unit of page content.

    Atomic is the operative word: the page chunker never splits an element
    across children, because a boundary violation degrades silently — the chunk
    still embeds, still matches, still returns, and is wrong.
    """

    element_type: str
    text: str
    order: int
    bbox: BBox | None = None
    #: Present for figures — a tenant-scoped object-storage path captured at
    #: parse time so a later vision channel does not require re-parsing.
    image_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Page:
    page_no: int
    elements: list[LayoutElement] = field(default_factory=list)
    image_ref: str | None = None

    def text(self) -> str:
        return "\n\n".join(el.text for el in self.elements if el.text)

    def is_empty(self) -> bool:
        return not any(el.text.strip() for el in self.elements)


@dataclass(slots=True)
class ParsedDocument:
    """Parse output.

    ``parent_unit`` is decided here and frozen onto the chunker version. It is
    never re-derived at query time: the same ``chunk_id`` resolving to different
    granularity depending on which code path read it is the disagreement defect
    that produced the confirmed cross-tenant index bug in the survey.
    """

    source_type: SourceType
    parser_version: str
    pages: list[Page] = field(default_factory=list)
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def parent_unit(self) -> str:
        return "page" if self.source_type in PAGINATED_SOURCES else "token_window"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def flat_text(self) -> str:
        """Document-global text.

        Character offsets in ``LayoutElement.metadata['char_offset']`` index
        into this string, which is what makes document-global citation spans
        resolvable.
        """
        return "\n\n".join(page.text() for page in self.pages)


def linearize_table(rows: list[list[str]], *, header: list[str] | None = None) -> str:
    """Render a table as text with the header bound to every row.

    Repeating the header is the whole point. A row without column names is a
    column of numbers that cannot be answered from, and embedding it that way
    produces a chunk that matches on the numbers while omitting what they mean.
    """
    if not rows:
        return ""
    header_row = header if header is not None else rows[0]
    body = rows[1:] if header is None else rows
    if not header_row:
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in body)

    header_cells = [cell.strip() for cell in header_row]
    lines = [" | ".join(header_cells)]
    for row in body:
        cells = [cell.strip() for cell in row]
        # Pad or truncate so every row lines up under its header.
        if len(cells) < len(header_cells):
            cells += [""] * (len(header_cells) - len(cells))
        elif len(cells) > len(header_cells):
            cells = cells[: len(header_cells)]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def parse_markdown(text: str, *, parser_version: str = "md-v1") -> ParsedDocument:
    """Parse Markdown into elements, splitting on heading structure.

    Headings and their bodies stay separate elements, which is what lets the
    chunker keep a heading with the section it introduces instead of separating
    them at a token boundary.
    """
    document = ParsedDocument(source_type=SourceType.MARKDOWN, parser_version=parser_version)
    current_page = Page(page_no=1)
    buffer: list[str] = []
    order = 0

    def flush() -> None:
        nonlocal order, buffer
        body = "\n".join(buffer).strip()
        if body:
            current_page.elements.append(
                LayoutElement(element_type="text", text=body, order=order)
            )
            order += 1
        buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            current_page.elements.append(
                LayoutElement(
                    element_type="heading",
                    text=stripped.lstrip("#").strip(),
                    order=order,
                )
            )
            order += 1
        elif stripped.startswith("|") and stripped.endswith("|"):
            flush()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):
                current_page.elements.append(
                    LayoutElement(
                        element_type="table",
                        text=linearize_table([cells]),
                        order=order,
                    )
                )
                order += 1
        elif stripped.startswith("- ") or stripped.startswith("* "):
            buffer.append(line)
        else:
            buffer.append(line)
    flush()

    document.pages = [current_page]
    return document


def parse_plain(text: str, *, parser_version: str = "txt-v1") -> ParsedDocument:
    """Parse unstructured text as a single page of paragraph elements.

    Treated as ``token_window`` parents — there is no page structure to anchor
    to, and pretending otherwise would produce parents of arbitrary length.
    """
    document = ParsedDocument(source_type=SourceType.PLAIN, parser_version=parser_version)
    elements = []
    order = 0
    for para in text.split("\n\n"):
        stripped = para.strip()
        if not stripped:
            continue
        elements.append(LayoutElement(element_type="text", text=stripped, order=order))
        order += 1
    document.pages = [Page(page_no=1, elements=elements)]
    return document


def parse_pdf_pages(
    pages: list[list[LayoutElement]],
    *,
    parser_version: str = "pdf-v1",
    title: str | None = None,
) -> ParsedDocument:
    """Wrap already-extracted per-page elements into a ``ParsedDocument``.

    Extraction itself is delegated to a PDF library at the ingest seam; this
    function owns the shape the rest of the pipeline depends on, including the
    invariant that page numbers are 1-based and contiguous.
    """
    document = ParsedDocument(
        source_type=SourceType.PDF, parser_version=parser_version, title=title
    )
    normalized: list[Page] = []
    for index, elements in enumerate(pages, start=1):
        for order, element in enumerate(elements):
            element.order = order
        normalized.append(Page(page_no=index, elements=list(elements)))
    document.pages = normalized
    return document


def assign_char_offsets(document: ParsedDocument) -> ParsedDocument:
    """Stamp each element with its document-global char offset.

    Computed by walking the flattened text once with an advancing cursor, not by
    searching from offset zero. With repeated or overlapping content a naive
    search resolves duplicate text to the wrong offset, and citations then point
    at the wrong span.
    """
    cursor = 0
    for page in document.pages:
        for element in page.elements:
            element.metadata["char_offset"] = cursor
            element.metadata["page_offset"] = _page_offset(page, element)
            cursor += len(element.text) + 2  # "\n\n" joiner
    document.metadata["char_length"] = cursor
    return document


def _page_offset(page: Page, target: LayoutElement) -> int:
    offset = 0
    for element in page.elements:
        if element is target:
            return offset
        offset += len(element.text) + 2
    return offset


def validate(document: ParsedDocument) -> None:
    if not document.pages:
        raise ValidationError(
            "parsed document has no pages", parser_version=document.parser_version
        )
    expected = 1
    for page in document.pages:
        if page.page_no != expected:
            raise ValidationError(
                "page numbers must be 1-based and contiguous",
                expected=expected,
                observed=page.page_no,
            )
        expected += 1