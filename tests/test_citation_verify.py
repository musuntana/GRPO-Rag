"""Citation verification: structural then semantic (architecture §8.6).

The claim under test is that a citation which was never checked is not a
citation. A hallucinated ``page_id`` and an unsupported claim both have to be
caught, at different layers, for different reasons.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from ragos.retrieval.citation import Citation, CitationStatus, EvidenceSet, bind_markers
from ragos.retrieval.types import ScoredChunk
from ragos.retrieval.verify import (
    SemanticVerdict,
    StructuralVerdict,
    citation_counts,
    verify_citations,
    verify_structural,
)


def _chunk(text: str, page: int | None = 1, page_count: int = 20) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid4(),
        doc_id=uuid4(),
        kb_id=uuid4(),
        text=text,
        score=0.9,
        channel="dense",
        page_no=page,
        page_count=page_count,
        start_char_idx=0,
        end_char_idx=len(text),
    )


def _evidence(*chunks: ScoredChunk) -> EvidenceSet:
    return EvidenceSet(
        chunks=tuple(chunks),
        page_counts={c.doc_id: c.page_count for c in chunks if c.doc_id and c.page_count},
    )


def _citation(chunk: ScoredChunk, claim: str = "a claim", **overrides) -> Citation:
    fields = {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "page_no": chunk.page_no,
        "start_char_idx": chunk.start_char_idx,
        "end_char_idx": chunk.end_char_idx,
        "claim": claim,
        "text": chunk.text,
    }
    fields.update(overrides)
    return Citation(**fields)


# -- structural ------------------------------------------------------------


def test_citation_to_a_chunk_outside_the_delivered_evidence_fails() -> None:
    delivered = _chunk("Revenue Q2 was 4M.")
    never_shown = _chunk("Revenue Q1 was 3M.")
    evidence = _evidence(delivered)

    result = verify_structural(_citation(never_shown), evidence)

    assert result.verdict == StructuralVerdict.UNKNOWN_CHUNK
    assert not result.ok


def test_citation_to_a_real_page_outside_the_document_range_fails() -> None:
    chunk = _chunk("text", page=99, page_count=20)
    evidence = _evidence(chunk)

    result = verify_structural(_citation(chunk), evidence)

    assert result.verdict == StructuralVerdict.PAGE_OUT_OF_RANGE


def test_malformed_span_fails() -> None:
    chunk = _chunk("text")
    evidence = _evidence(chunk)

    result = verify_structural(_citation(chunk, start_char_idx=50, end_char_idx=10), evidence)

    assert result.verdict == StructuralVerdict.MALFORMED_SPAN


def test_marker_beyond_the_delivered_set_fails_even_though_the_text_exists() -> None:
    """A wrong pointer is not verifiable, even when the cited text is right."""
    first, second = _chunk("a"), _chunk("b")
    answer = "The claim [1]. The bad claim [9]."
    evidence = _evidence(first, second)
    citations = bind_markers(answer, evidence)

    verified = asyncio.run(verify_citations(citations, evidence))

    assert verified[0].marker_out_of_range is None
    assert verified[1].marker_out_of_range == 2
    assert verified[1].status_reason == StructuralVerdict.MARKER_OUT_OF_RANGE.value


# -- semantic ---------------------------------------------------------------


def _always(verdict: SemanticVerdict):
    async def entail(claim: str, text: str) -> SemanticVerdict:
        return verdict

    return entail


def test_semantic_verification_marks_entailed_citations() -> None:
    chunk = _chunk("Revenue Q2 was 4M.")
    citations = bind_markers("Revenue was 4M [1].", _evidence(chunk))
    evidence = _evidence(chunk)

    verified = asyncio.run(
        verify_citations(citations, evidence, entailment=_always(SemanticVerdict.ENTAILED))
    )

    assert verified[0].status == CitationStatus.VERIFIED
    assert verified[0].status_reason == "entailed"


def test_semantic_verification_marks_contradicted_citations_unverified() -> None:
    chunk = _chunk("Revenue Q2 was 4M.")
    citations = bind_markers("Revenue was 99M [1].", _evidence(chunk))
    evidence = _evidence(chunk)

    verified = asyncio.run(
        verify_citations(citations, evidence, entailment=_always(SemanticVerdict.CONTRADICTED))
    )

    assert verified[0].status == CitationStatus.UNVERIFIED
    assert verified[0].status_reason == "contradicted"


def test_structural_failure_never_reaches_the_entailment_judge() -> None:
    """A judge must not be asked to vouch for a span the user cannot navigate to."""
    delivered = _chunk("delivered")
    never_shown = _chunk("never shown")
    citations = [_citation(never_shown)]

    called: list[tuple[str, str]] = []

    async def tracking_entail(claim: str, text: str) -> SemanticVerdict:
        called.append((claim, text))
        return SemanticVerdict.ENTAILED

    asyncio.run(
        verify_citations(citations, _evidence(delivered), entailment=tracking_entail)
    )

    assert called == [], "entailment was called for a structurally invalid citation"


def test_semantic_verification_is_skipped_when_no_entailment_is_supplied() -> None:
    """Online default: an unpriced per-claim LLM call is a cost defect."""
    chunk = _chunk("Revenue Q2 was 4M.")
    citations = bind_markers("Revenue was 4M [1].", _evidence(chunk))

    verified = asyncio.run(verify_citations(citations, _evidence(chunk)))

    assert verified[0].status == CitationStatus.UNVERIFIED
    assert verified[0].status_reason == "structural_ok_semantic_skipped"


# -- metric denominators ----------------------------------------------------


def test_citation_counts_excludes_suppressed_from_the_denominator() -> None:
    chunk = _chunk("text")
    verified = _citation(chunk, status=CitationStatus.VERIFIED)
    unverified = _citation(chunk, status=CitationStatus.UNVERIFIED)
    suppressed = _citation(chunk, status=CitationStatus.SUPPRESSED)

    supported, generated = citation_counts([verified, unverified, suppressed])

    # The suppressed citation was never delivered; counting it would let a
    # system improve its ratio by hiding citations.
    assert (supported, generated) == (1, 2)


def test_citation_accuracy_is_zero_when_nothing_was_cited() -> None:
    assert citation_counts([]) == (0, 0)


def test_marker_indexes_are_one_based_over_delivered_evidence() -> None:
    first, second = _chunk("a"), _chunk("b")
    evidence = _evidence(first, second)

    citations = bind_markers("First [1]. Second [2].", evidence)

    assert citations[0].chunk_id == first.chunk_id
    assert citations[1].chunk_id == second.chunk_id


def test_unmarked_claim_is_bound_but_left_unattributed() -> None:
    chunk = _chunk("a")
    evidence = _evidence(chunk)

    citations = bind_markers("A claim with no marker.", evidence)

    assert citations[0].marker_index == -1
    assert citations[0].chunk_id == chunk.chunk_id


@pytest.mark.parametrize(
    "answer,expected_marker",
    [
        ("Claim [2].", 2),
        ("Claim 【3】.", 3),
        ("Claim with no marker.", -1),
    ],
)
def test_marker_formats_are_parsed(answer: str, expected_marker: int) -> None:
    from ragos.retrieval.citation import find_marker

    assert find_marker(answer) == expected_marker