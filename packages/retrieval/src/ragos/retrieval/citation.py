"""Citation objects and extraction.

Architecture §8.6: citations are produced, then verified before delivery. A
citation that was never checked is not a citation — it is a guess with a
pointer attached.

The citation carries the span, the page, the claim, and the verification
verdict. There is deliberately no separate verification log: a verdict that
lives somewhere other than the object it describes can drift from what was
delivered.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from ragos.retrieval.types import ScoredChunk


class CitationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    SUPPRESSED = "suppressed"


@dataclass(slots=True)
class Citation:
    """One claim, the span that supports it, and whether it actually does.

    ``page_no`` is the navigation affordance; the span is the precision
    guarantee. Neither replaces the other — a page without a span cannot be
    checked against a specific figure, and a span without a page cannot be
    found by someone holding the PDF.
    """

    chunk_id: UUID
    doc_id: UUID | None
    page_no: int | None
    start_char_idx: int | None
    end_char_idx: int | None
    claim: str
    text: str
    score: float = 0.0
    element_type: str = "text"
    status: CitationStatus = CitationStatus.UNVERIFIED
    status_reason: str | None = None

    #: 1-based index into the delivered evidence, or ``-1`` when the claim
    #: carried no marker. Retained so an out-of-range marker is detectable
    #: rather than silently resolved to some other evidence item.
    marker_index: int = -1

    #: ``None`` when the marker resolved cleanly. Set to the evidence count when
    #: a marker named an item past the end of the delivered set.
    marker_out_of_range: int | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "doc_id": str(self.doc_id) if self.doc_id else None,
            "page_no": self.page_no,
            "start_char_idx": self.start_char_idx,
            "end_char_idx": self.end_char_idx,
            "claim": self.claim,
            "text": self.text,
            "score": self.score,
            "element_type": self.element_type,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "marker_index": self.marker_index,
        }


#: ``[3]``, ``[chunk:abc]``, ``(p. 12)``, ``【3】``. The numeric form is the one
#: a grounded prompt is instructed to emit.
_MARKER_PATTERNS = (
    re.compile(r"\[(\d+)\]"),
    re.compile(r"【(\d+)】"),
    re.compile(r"\[chunk:([0-9a-fA-F-]{36})\]"),
)


@dataclass(slots=True)
class ExtractedClaim:
    """One claim plus the 1-based evidence marker it cited, or ``-1`` if none.

    A claim with no marker is still returned — it is precisely the unattributed
    claim that ``citation_completeness`` counts.
    """

    claim: str
    marker_index: int = -1
    chunk_id: UUID | None = None


def find_marker(sentence: str) -> int:
    """Return the 1-based evidence marker in a sentence, or ``-1``."""
    for pattern in _MARKER_PATTERNS:
        match = pattern.search(sentence)
        if match is not None:
            return int(match.group(1))
    return -1


def find_chunk_marker(sentence: str) -> UUID | None:
    """Return an explicit ``[chunk:<uuid>]`` marker, if present."""
    match = _MARKER_PATTERNS[2].search(sentence)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:
        return None


def extract_claims(answer: str) -> list[ExtractedClaim]:
    """Split an answer into claims with their citation markers."""
    claims: list[ExtractedClaim] = []
    for sentence in _split_sentences(answer):
        text = sentence.strip()
        if not text:
            continue
        claims.append(
            ExtractedClaim(
                claim=text,
                marker_index=find_marker(text),
                chunk_id=find_chunk_marker(text),
            )
        )
    return claims


def bind_markers(
    answer: str,
    evidence: EvidenceSet | Sequence[ScoredChunk],
) -> list[Citation]:
    """Build citations from an answer and the evidence actually delivered.

    ``marker_index`` is 1-based over the evidence list, so ``[3]`` names the
    third item the generator was given. A marker outside that range falls back
    to the first item and stays ``unverified``, which structural verification
    catches — a marker pointing at a page that was never in the context is
    exactly the hallucinated-citation case, and silently binding it to
    *something* would hide it.
    """
    items = evidence.chunks if isinstance(evidence, EvidenceSet) else tuple(evidence)
    citations: list[Citation] = []
    for sentence in _split_sentences(answer):
        text = sentence.strip()
        if not text:
            continue
        marker_index: int | None = None
        for pattern in _MARKER_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            if pattern is _MARKER_PATTERNS[2]:
                continue
            marker_index = int(match.group(1))
            break

        out_of_range: int | None = None
        if marker_index is not None and 1 <= marker_index <= len(items):
            src = items[marker_index - 1]
        elif marker_index is not None:
            # Marker named an item past the end of the delivered set. Bound to
            # the first item only so the span is renderable; the out-of-range
            # marker is recorded and structural verification rejects it.
            src = items[0] if items else None
            out_of_range = len(items)
        else:
            src = items[0] if items else None

        citations.append(
            Citation(
                chunk_id=src.chunk_id if src else UUID(int=0),
                doc_id=src.doc_id if src else None,
                page_no=src.page_no if src else None,
                start_char_idx=src.start_char_idx if src else None,
                end_char_idx=src.end_char_idx if src else None,
                claim=text,
                text=src.text if src else "",
                score=src.score if src else 0.0,
                element_type=src.element_type if src else "text",
                marker_index=marker_index if marker_index is not None else -1,
                marker_out_of_range=out_of_range,
            )
        )
    return citations


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in ".!?。！？\n":
            sentence = "".join(buf).strip()
            if sentence:
                out.append(sentence)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


@dataclass(slots=True)
class EvidenceSet:
    """The exact evidence delivered to the generator.

    Verification checks membership against this object rather than against the
    corpus, because the question is not "does this chunk exist" but "was this
    chunk in the context the model was given".
    """

    chunks: tuple[ScoredChunk, ...]
    page_counts: dict[UUID, int] = field(default_factory=dict)

    def chunk_ids(self) -> set[UUID]:
        return {c.chunk_id for c in self.chunks}

    def by_id(self, chunk_id: UUID) -> ScoredChunk | None:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None