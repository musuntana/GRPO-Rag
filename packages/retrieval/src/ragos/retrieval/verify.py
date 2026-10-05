"""Two-layer citation verification (architecture §8.6).

    generation
      -> citation extraction
      -> STRUCTURAL verification   deterministic, always on, zero LLM cost
      -> SEMANTIC verification     entailment; always in eval, opt-in online
      -> delivery

The split exists because semantic verification is an LLM call per claim. A
hallucinated ``chunk_id`` has to be caught deterministically *before* it reaches
a judge that could be talked into accepting it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ragos.retrieval.citation import Citation, CitationStatus, EvidenceSet


class StructuralVerdict(StrEnum):
    OK = "ok"
    UNKNOWN_CHUNK = "unknown_chunk"
    MARKER_OUT_OF_RANGE = "marker_out_of_range"
    PAGE_OUT_OF_RANGE = "page_out_of_range"
    MALFORMED_SPAN = "malformed_span"


class SemanticVerdict(StrEnum):
    ENTAILED = "entailed"
    CONTRADICTED = "contradicted"
    NOT_ENOUGH_INFO = "not_enough_info"


#: Signature of the entailment check. Injected so the same verification code
#: runs against an NLI model in evaluation and an LLM judge online, and so tests
#: do not need a provider.
EntailmentFn = Callable[[str, str], Awaitable[SemanticVerdict]]


@dataclass(slots=True)
class StructuralResult:
    verdict: StructuralVerdict
    citation: Citation

    @property
    def ok(self) -> bool:
        return self.verdict == StructuralVerdict.OK


def verify_structural(citation: Citation, evidence: EvidenceSet) -> StructuralResult:
    """Check the citation against the evidence actually delivered.

    Fails closed. The question is not "does this chunk exist somewhere in the
    corpus" but "was this chunk in the context the model was handed" — a
    citation to a page that was never retrieved is a hallucinated citation even
    when the page is real.
    """
    if citation.marker_out_of_range is not None:
        # The generator cited an item that was never delivered. The bound chunk
        # may well be relevant, but the *reference* is wrong, and a citation
        # whose pointer is wrong cannot be checked by a user.
        return StructuralResult(StructuralVerdict.MARKER_OUT_OF_RANGE, citation)

    if citation.chunk_id not in evidence.chunk_ids():
        return StructuralResult(StructuralVerdict.UNKNOWN_CHUNK, citation)

    if citation.page_no is not None:
        page_count = evidence.page_counts.get(citation.doc_id) if citation.doc_id else None
        if page_count is not None and not (1 <= citation.page_no <= page_count):
            return StructuralResult(StructuralVerdict.PAGE_OUT_OF_RANGE, citation)

    start = citation.start_char_idx
    end = citation.end_char_idx
    if start is not None and end is not None:
        if start < 0 or end <= start:
            return StructuralResult(StructuralVerdict.MALFORMED_SPAN, citation)

    return StructuralResult(StructuralVerdict.OK, citation)


def verify_structural_batch(
    citations: list[Citation], evidence: EvidenceSet
) -> list[StructuralResult]:
    return [verify_structural(c, evidence) for c in citations]


async def verify_semantic(
    citations: list[Citation],
    *,
    entailment: EntailmentFn,
) -> list[Citation]:
    """Check whether each cited span entails the claim attributed to it.

    Only citations that passed structural verification reach this layer.
    """
    verified: list[Citation] = []
    for citation in citations:
        if citation.status == CitationStatus.SUPPRESSED:
            verified.append(citation)
            continue
        verdict = await entailment(citation.claim, citation.text)
        if verdict == SemanticVerdict.ENTAILED:
            citation.status = CitationStatus.VERIFIED
            citation.status_reason = "entailed"
        else:
            citation.status = CitationStatus.UNVERIFIED
            citation.status_reason = verdict.value
        verified.append(citation)
    return verified


async def verify_citations(
    citations: list[Citation],
    evidence: EvidenceSet,
    *,
    entailment: EntailmentFn | None = None,
) -> list[Citation]:
    """Run both layers.

    Structural failures are marked and left for the caller to suppress or
    surface. Semantic verification is skipped entirely when no entailment
    function is supplied, which is the online default — an unpriced per-claim
    LLM call on the request path is a cost defect, not a feature.
    """
    results = verify_structural_batch(citations, evidence)
    survivors: list[Citation] = []

    for result in results:
        citation = result.citation
        citation.status = CitationStatus.UNVERIFIED
        if result.ok:
            survivors.append(citation)
        else:
            # A structurally invalid citation never reaches the judge. Asking an
            # LLM whether the text supports a claim whose pointer is wrong would
            # invite it to answer about a span the user cannot navigate to.
            citation.status_reason = result.verdict.value

    if entailment is None:
        for citation in survivors:
            citation.status_reason = "structural_ok_semantic_skipped"
        return results_as_list(results)

    verified = await verify_semantic(survivors, entailment=entailment)
    return _merge(results, verified)


def _merge(
    results: list[StructuralResult],
    verified: list[Citation],
) -> list[Citation]:
    """Reassemble in original order, taking the semantic verdict where one exists."""
    by_key = {(c.chunk_id, c.marker_index): c for c in verified}
    out: list[Citation] = []
    for result in results:
        key = (result.citation.chunk_id, result.citation.marker_index)
        out.append(by_key.get(key, result.citation))
    return out


def results_as_list(results: list[StructuralResult]) -> list[Citation]:
    return [r.citation for r in results]


def split_by_status(citations: list[Citation]) -> dict[CitationStatus, list[Citation]]:
    grouped: dict[CitationStatus, list[Citation]] = {
        status: [] for status in CitationStatus
    }
    for citation in citations:
        grouped[citation.status].append(citation)
    return grouped


def citation_counts(citations: list[Citation]) -> tuple[int, int]:
    """``(supported, generated)`` — the numerator and denominator of §8.6.

    ``supported`` counts only verified citations. A suppressed citation is not
    in the denominator: it was never delivered, so counting it would flatter the
    ratio.
    """
    generated = sum(1 for c in citations if c.status != CitationStatus.SUPPRESSED)
    supported = sum(1 for c in citations if c.status == CitationStatus.VERIFIED)
    return supported, generated


def empty_evidence() -> EvidenceSet:
    return EvidenceSet(chunks=())


def chunk_ids(citations: list[Citation]) -> set[UUID]:
    return {c.chunk_id for c in citations}