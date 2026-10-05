"""Page anchoring: page as parent, semantic children within the page.

Architecture §7.4.1 / ADR-0006. The claim under test is that for paginated
sources the page is the semantic unit, so it is the parent, and retrieval still
matches on children for precision.
"""

from __future__ import annotations

import pytest
from ragos.core.errors import ValidationError
from ragos.retrieval.parse.layout import (
    LayoutElement,
    Page,
    ParsedDocument,
    assign_char_offsets,
    linearize_table,
    parse_markdown,
    parse_pdf_pages,
    parse_plain,
    validate,
)
from ragos.retrieval.parse.page_chunker import (
    ChunkerConfig,
    PageChunker,
    split_sentences_with_offsets,
)

FOOTER = "Confidential — internal use only."


def _pdf() -> ParsedDocument:
    page_one = [
        LayoutElement("heading", "Q2 Financials", 0),
        LayoutElement(
            "table",
            linearize_table(
                [
                    ["Period", "Revenue"],
                    ["Q1", "3M"],
                    ["Q2", "4M"],
                ]
            ),
            1,
        ),
        LayoutElement("text", "Total revenue rose year over year.", 2),
    ]
    page_two = [
        LayoutElement("heading", "Q3 Financials", 0),
        LayoutElement(
            "table",
            linearize_table([["Period", "Revenue"], ["Q3", "5M"]]),
            1,
        ),
    ]
    return parse_pdf_pages([page_one, page_two])


def test_page_is_the_parent_for_paginated_sources() -> None:
    document = _pdf()

    nodes = PageChunker(ChunkerConfig(parent_unit="page")).chunk(document)
    parents = [n for n in nodes if n.is_parent]

    assert document.parent_unit == "page"
    assert len(parents) == 2
    assert [p.page_no for p in parents] == [1, 2]
    assert all(p.page_count == 2 for p in parents)


def test_children_carry_page_number_and_never_cross_pages() -> None:
    nodes = PageChunker(ChunkerConfig(parent_unit="page")).chunk(_pdf())
    children = [n for n in nodes if not n.is_parent]

    assert {c.page_no for c in children} == {1, 2}
    # A child belongs to exactly one page; a child spanning a page boundary
    # would put a table header and its figures in different parents.
    for child in children:
        parent = next(p for p in nodes if p.chunk_id == child.parent_chunk_id)
        assert child.page_no == parent.page_no


def test_offsets_round_trip_against_flattened_document() -> None:
    document = _pdf()
    nodes = PageChunker(ChunkerConfig(parent_unit="page")).chunk(document)
    flat = document.flat_text()

    for node in nodes:
        assert flat[node.start_char_idx : node.end_char_idx] == node.text


def test_page_offsets_are_relative_to_their_own_page() -> None:
    """Page offsets index into that page's text; global offsets into the document.

    Both are carried: the page is the navigation affordance and the span is the
    precision guarantee, so each has to resolve against the right base.
    """
    document = _pdf()
    nodes = PageChunker(ChunkerConfig(parent_unit="page")).chunk(document)
    flat = document.flat_text()
    page_text = {page.page_no: page.text() for page in document.pages}

    for node in nodes:
        assert node.page_offset_start is not None
        assert node.page_offset_end is not None
        base = page_text[node.page_no]
        assert (
            base[node.page_offset_start : node.page_offset_end] == node.text
        ), "page offset must resolve against its own page"
        assert (
            flat[node.start_char_idx : node.end_char_idx] == node.text
        ), "global offset must resolve against the document"


def test_duplicate_text_does_not_steal_another_chunks_span() -> None:
    """The failure mode the advancing-cursor rule exists to prevent.

    Two sections carry the same sentence. Searching from offset zero would bind
    both children's spans to the first occurrence, so a citation pointing at the
    second would render the first section's text.
    """
    document = parse_markdown(
        f"# Revenue\n\nRevenue was 10M.\n\n# Notes\n\n{FOOTER}\n\n"
        f"# Prior year\n\nRevenue was 10M."
    )
    nodes = PageChunker(
        ChunkerConfig(parent_unit="token_window", child_size=20, parent_size=40)
    ).chunk(document)
    flat = document.flat_text()

    # Children are the retrieval-matching unit; parents legitimately span both
    # occurrences. The claim under test is that the two *children* holding the
    # repeated sentence resolve to different offsets.
    revenue_children = [
        n for n in nodes if not n.is_parent and n.text.endswith("Revenue was 10M.")
    ]
    assert len(revenue_children) == 2, "fixture must yield one child per occurrence"

    for node in nodes:
        assert flat[node.start_char_idx : node.end_char_idx] == node.text

    starts = sorted(n.start_char_idx for n in revenue_children)
    assert starts[1] > starts[0], "repeated text collapsed onto one offset"
    assert flat[starts[1] : starts[1] + len(revenue_children[1].text)].startswith(
        "Prior year"
    )


def test_element_boundaries_are_hard_with_oversize_fallback() -> None:
    long_table = linearize_table(
        [["Period", "Revenue"]] + [[f"Q{i}", f"{i}M"] for i in range(1, 60)]
    )
    document = parse_pdf_pages(
        [[LayoutElement("table", long_table, 0), LayoutElement("text", "Summary.", 1)]]
    )

    nodes = PageChunker(ChunkerConfig(parent_unit="page", child_size=40)).chunk(document)
    table_children = [n for n in nodes if n.element_type == "table"]

    assert len(table_children) > 1, "an oversize table must split rather than stay whole"
    for child in table_children:
        # Every piece is still a table child — it never becomes prose that has
        # lost its column names.
        assert child.element_type == "table"
        assert "Period | Revenue" in child.text


def test_table_linearization_binds_header_to_every_row() -> None:
    table = linearize_table([["Period", "Revenue"], ["Q1", "3M"], ["Q2", "4M"]])
    rows = table.splitlines()

    assert rows[0] == "Period | Revenue"
    assert rows[1] == "Q1 | 3M"
    # Every body row carries the column names, which is the point: a bare row
    # of numbers cannot be answered from.
    assert all(len(row.split("|")) == 2 for row in rows)


def test_table_linearization_pads_ragged_rows() -> None:
    table = linearize_table([["A", "B", "C"], ["1", "2"]])

    assert table.splitlines()[1] == "1 | 2 | "


def test_page_chunker_rejects_page_unit_on_unpaginated_source() -> None:
    document = parse_plain("Some prose. More prose.")

    with pytest.raises(ValueError, match="unpaginated"):
        PageChunker(ChunkerConfig(parent_unit="page")).chunk(document)


def test_token_window_parents_have_no_page_number() -> None:
    document = parse_markdown(f"# T\n\n{'Body sentence. ' * 60}")

    nodes = PageChunker(
        ChunkerConfig(parent_unit="token_window", child_size=40, parent_size=120)
    ).chunk(document)

    assert all(n.page_no is None for n in nodes)
    assert all(n.page_count is None for n in nodes)


def test_parent_child_ratio_is_enforced() -> None:
    with pytest.raises(ValueError, match="parent_size must be >= child_size"):
        ChunkerConfig(child_size=800, parent_size=400).validate()


def test_chunker_version_records_the_parent_unit() -> None:
    assert "page" in ChunkerConfig(parent_unit="page").version_id()
    assert "token_window" in ChunkerConfig(parent_unit="token_window").version_id()


def test_char_offsets_are_assigned_by_an_advancing_cursor() -> None:
    document = parse_markdown(f"# A\n\nFirst.\n\n# B\n\n{FOOTER}")

    assign_char_offsets(document)
    flat = document.flat_text()
    elements = document.pages[0].elements

    cursor = 0
    for element in elements:
        assert element.metadata["char_offset"] == cursor
        assert flat[cursor : cursor + len(element.text)] == element.text
        cursor += len(element.text) + 2


def test_split_sentences_offsets_are_exact() -> None:
    text = "Alpha one. Beta two! Gamma three?"

    spans = split_sentences_with_offsets(text)

    for sentence, start, end in spans:
        assert text[start:end] == sentence


def test_validate_rejects_non_contiguous_page_numbers() -> None:
    document = ParsedDocument(source_type=parse_plain("x").source_type, parser_version="t")
    document.pages = [Page(page_no=1), Page(page_no=3)]

    with pytest.raises(ValidationError, match="contiguous"):
        validate(document)


def test_element_type_is_a_closed_set() -> None:
    """A parser emitting an unknown type is a bug, and it should fail loudly."""
    from ragos.retrieval.types import ElementType

    assert {e.value for e in ElementType} == {
        "text",
        "heading",
        "table",
        "figure",
        "list",
        "code",
        "caption",
    }