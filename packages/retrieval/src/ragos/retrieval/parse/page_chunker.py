"""Page-anchored and token-window chunkers.

Two strategies, selected by a value recorded on ``chunker_version`` and never
inferred at query time (architecture §7.4.1, ADR-0006):

``parent_unit = "page"``
    Paginated sources. The page is the parent; children are semantic spans
    within it. Retrieval still matches on children for precision; expansion
    resolves to the page, so the generation context is a whole financial page or
    a whole slide rather than an arbitrary 1600 tokens of one.

``parent_unit = "token_window"``
    Flat text. Parent is a token window, unchanged from architecture §7.4.

Element boundaries are hard in both modes: a child never spans a boundary
between element types, and an element larger than the child budget becomes one
oversize child rather than a broken one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from ragos.retrieval.parse.layout import (
    LayoutElement,
    ParsedDocument,
    assign_char_offsets,
    validate,
)
from ragos.retrieval.types import ElementType, ParentUnit

#: A cross-language packing proxy; real provider token budgets remain authoritative.
_SENTENCE_END = ".!?。！？"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(len(text.split()), len(text) // 3)


def split_sentences(text: str) -> list[str]:
    return [s for s, _, _ in split_sentences_with_offsets(text)]


def split_sentences_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """Split into ``(sentence, start, end)`` against the original string.

    Offsets come from the walk itself rather than from a later search. Joining
    sentences back with a single space and then locating the result in the
    document does not round-trip when the original whitespace differed, and a
    failed lookup silently falls back to a wrong span — which is exactly the
    corruption the advancing-cursor rule exists to prevent.
    """
    out: list[tuple[str, int, int]] = []
    start: int | None = None
    for idx, ch in enumerate(text):
        if start is None:
            if not ch.isspace():
                start = idx
        if ch in _SENTENCE_END:
            if start is not None:
                out.append((text[start:idx + 1], start, idx + 1))
            start = None
    if start is not None and start < len(text):
        out.append((text[start:len(text)], start, len(text)))
    return out


@dataclass(slots=True)
class ChunkNode:
    """One chunk in the document's chunk tree.

    Under ``parent_unit = "page"`` the parent node carries ``page_no`` and the
    children carry ``page_offset_start`` / ``page_offset_end`` alongside the
    document-global offsets that citation linking depends on.
    """

    chunk_id: UUID
    parent_chunk_id: UUID | None
    is_parent: bool
    text: str
    token_count: int
    order: int

    page_no: int | None = None
    page_count: int | None = None
    page_offset_start: int | None = None
    page_offset_end: int | None = None
    start_char_idx: int | None = None
    end_char_idx: int | None = None
    element_type: str = ElementType.TEXT.value
    layout_bbox: dict[str, float] | None = None
    page_image_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_search_fields(self) -> dict[str, Any]:
        return {
            "page_no": self.page_no,
            "page_count": self.page_count,
            "page_offset_start": self.page_offset_start,
            "page_offset_end": self.page_offset_end,
            "start_char_idx": self.start_char_idx,
            "end_char_idx": self.end_char_idx,
            "element_type": self.element_type,
            "page_image_ref": self.page_image_ref,
        }


def _pack(sentences: list[str], target: int) -> list[list[str]]:
    buckets: list[list[str]] = []
    current: list[str] = []
    total = 0
    for sentence in sentences:
        size = estimate_tokens(sentence)
        if current and total + size > target:
            buckets.append(current)
            current = []
            total = 0
        current.append(sentence)
        total += size
    if current:
        buckets.append(current)
    return buckets


def _pack_spans(
    spans: list[tuple[str, int, int]], target: int
) -> list[list[tuple[str, int, int]]]:
    """Greedy-pack ``(text, start, end)`` spans, preserving their offsets.

    A span is a contiguous slice of the document, so packing keeps the outer
    bounds of the first and last span in each bucket.
    """
    buckets: list[list[tuple[str, int, int]]] = []
    current: list[tuple[str, int, int]] = []
    total = 0
    for span in spans:
        size = estimate_tokens(span[0])
        if current and total + size > target:
            buckets.append(current)
            current = []
            total = 0
        current.append(span)
        total += size
    if current:
        buckets.append(current)
    return buckets


@dataclass(slots=True)
class ChunkerConfig:
    child_size: int = 400
    parent_size: int = 1600
    parent_unit: ParentUnit = "token_window"
    parser_version: str = "md-v1"

    def validate(self) -> None:
        if self.child_size <= 0 or self.parent_size <= 0:
            raise ValueError("child_size and parent_size must be positive")
        if self.parent_size < self.child_size:
            raise ValueError("parent_size must be >= child_size")
        if self.parent_unit not in ("page", "token_window"):
            raise ValueError(f"unknown parent_unit {self.parent_unit!r}")

    def version_id(self) -> str:
        return (
            f"{self.parser_version}:{self.parent_unit}:"
            f"c{self.child_size}:p{self.parent_size}"
        )


class PageChunker:
    """Chunk a parsed document under a recorded parent unit."""

    def __init__(self, config: ChunkerConfig | None = None) -> None:
        self.config = config or ChunkerConfig()
        self.config.validate()

    def chunk(self, document: ParsedDocument) -> list[ChunkNode]:
        validate(document)
        assign_char_offsets(document)

        parent_unit = self.config.parent_unit
        if parent_unit == "page":
            if document.parent_unit != "page":
                # Recorded, not inferred — a mismatch means the config and the
                # index disagree about granularity, which must fail loudly
                # rather than silently produce the wrong hierarchy.
                raise ValueError(
                    "chunker configured parent_unit='page' but source "
                    f"{document.source_type.value} is unpaginated"
                )
            return self._chunk_by_page(document)
        return self._chunk_by_token_window(document)

    # -- page parents ----------------------------------------------------

    def _chunk_by_page(self, document: ParsedDocument) -> list[ChunkNode]:
        page_count = document.page_count
        nodes: list[ChunkNode] = []
        order = 0

        for page in document.pages:
            page_text = page.text()
            page_offset = (
                int(page.elements[0].metadata.get("char_offset", 0)) if page.elements else 0
            )
            parent_id = uuid4()
            nodes.append(
                ChunkNode(
                    chunk_id=parent_id,
                    parent_chunk_id=None,
                    is_parent=True,
                    text=page_text,
                    token_count=estimate_tokens(page_text),
                    order=order,
                    page_no=page.page_no,
                    page_count=page_count,
                    page_offset_start=0,
                    page_offset_end=len(page_text),
                    start_char_idx=page_offset,
                    end_char_idx=page_offset + len(page_text),
                    element_type=ElementType.TEXT.value,
                    page_image_ref=page.image_ref,
                )
            )
            order += 1

            for child in self._children_of_page(page, page_count, page_offset, parent_id):
                child.order = order
                nodes.append(child)
                order += 1

        return nodes

    def _children_of_page(
        self,
        page: Any,
        page_count: int,
        page_char_offset: int,
        parent_id: UUID,
    ) -> list[ChunkNode]:
        children: list[ChunkNode] = []
        for element in page.elements:
            if not element.text.strip():
                continue
            element.metadata.setdefault("page_no", page.page_no)
            children.extend(
                self._children_for_element(element, page_count, parent_id)
            )
        return children

    def _children_for_element(
        self,
        element: LayoutElement,
        page_count: int,
        parent_id: UUID,
    ) -> list[ChunkNode]:
        char_offset = int(element.metadata.get("char_offset", 0))
        page_offset = int(element.metadata.get("page_offset", 0))
        bbox = element.bbox.as_dict() if element.bbox else None
        page_no = element.metadata.get("page_no")

        def make(text: str, *, offset_in_element: int, page_offset_in_element: int) -> ChunkNode:
            return ChunkNode(
                chunk_id=uuid4(),
                parent_chunk_id=parent_id,
                is_parent=False,
                text=text,
                token_count=estimate_tokens(text),
                order=0,
                page_no=page_no,
                page_count=page_count,
                page_offset_start=page_offset + page_offset_in_element,
                page_offset_end=page_offset + page_offset_in_element + len(text),
                start_char_idx=char_offset + offset_in_element,
                end_char_idx=char_offset + offset_in_element + len(text),
                element_type=element.element_type,
                layout_bbox=bbox,
                page_image_ref=element.image_ref,
            )

        if estimate_tokens(element.text) <= self.config.child_size:
            return [make(element.text, offset_in_element=0, page_offset_in_element=0)]

        # Oversize element: split *within* the element, never across into a
        # neighbour. Each piece keeps the element's type and bbox, so a large
        # table becomes several table children rather than prose that has lost
        # its column names.
        pieces: list[ChunkNode] = []
        for piece_text, offset_in_element in self._split_oversize(element):
            pieces.append(
                make(
                    piece_text,
                    offset_in_element=offset_in_element,
                    page_offset_in_element=offset_in_element,
                )
            )
        return pieces

    def _split_oversize(self, element: LayoutElement) -> list[tuple[str, int]]:
        """Split one element into ``(text, offset_in_element)`` pieces.

        A table splits on row boundaries and every piece repeats the header row,
        so the column names survive the split — the same invariant the
        linearizer established before chunking. Splitting a table on sentence
        boundaries instead would be meaningless, because a table has no
        sentences.
        """
        if element.element_type == "table" and "\n" in element.text:
            return self._split_table(element.text)

        pieces: list[tuple[str, int]] = []
        cursor = 0
        for group in _pack(split_sentences(element.text), self.config.child_size):
            piece = " ".join(group)
            offset = element.text.find(piece, cursor)
            if offset < 0:
                offset = cursor
            pieces.append((piece, offset))
            cursor = offset + len(piece)
        return pieces

    def _split_table(self, text: str) -> list[tuple[str, int]]:
        lines = text.split("\n")
        header, body = lines[0], lines[1:]

        pieces: list[tuple[str, int]] = []
        current: list[str] = []
        current_offset = len(header) + 1

        def flush(offset: int) -> None:
            if not current:
                return
            pieces.append((header + "\n" + "\n".join(current), offset))

        for line in body:
            line_offset = text.index(line, current_offset - 1 if pieces else 0)
            if line_offset < 0:
                line_offset = current_offset
            candidate_tokens = estimate_tokens("\n".join([*current, line]))
            if current and candidate_tokens > self.config.child_size:
                flush(current_offset)
                current = [line]
                current_offset = line_offset + len(line) + 1
            else:
                if not current:
                    current_offset = line_offset
                current.append(line)
        flush(current_offset)
        return pieces

    # -- token-window parents --------------------------------------------

    def _chunk_by_token_window(self, document: ParsedDocument) -> list[ChunkNode]:
        text = document.flat_text()
        if not text.strip():
            return []
        spans = split_sentences_with_offsets(text)
        nodes: list[ChunkNode] = []
        order = 0

        for parent_group in _pack_spans(spans, self.config.parent_size):
            parent_start = parent_group[0][1]
            parent_end = parent_group[-1][2]
            parent_text = text[parent_start:parent_end]
            parent_id = uuid4()
            nodes.append(
                ChunkNode(
                    chunk_id=parent_id,
                    parent_chunk_id=None,
                    is_parent=True,
                    text=parent_text,
                    token_count=estimate_tokens(parent_text),
                    order=order,
                    start_char_idx=parent_start,
                    end_char_idx=parent_end,
                    page_no=None,
                    page_count=None,
                )
            )
            order += 1

            # Children are packed from the parent's own spans and inherit its
            # exact document bounds — no searching, so repeated text elsewhere
            # in the document cannot steal this child's span.
            for child_group in _pack_spans(parent_group, self.config.child_size):
                child_start = child_group[0][1]
                child_end = child_group[-1][2]
                nodes.append(
                    ChunkNode(
                        chunk_id=uuid4(),
                        parent_chunk_id=parent_id,
                        is_parent=False,
                        text=text[child_start:child_end],
                        token_count=estimate_tokens(text[child_start:child_end]),
                        order=order,
                        start_char_idx=child_start,
                        end_char_idx=child_end,
                        page_no=None,
                        page_count=None,
                    )
                )
                order += 1

        return nodes


def page_of(node: ChunkNode) -> int | None:
    return node.page_no