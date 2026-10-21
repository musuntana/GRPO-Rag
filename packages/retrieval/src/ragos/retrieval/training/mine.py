"""Hard-negative mining (ADR-0007 stage 1).

The load-bearing decision here is that mined negatives are **graded, not
irrelevant**. Mining selects what the retriever already scores highly, so the
negative set skews to grade 1 — same document, adjacent page, cross-period
sibling. Those are the hardest and most useful training signals, and they are
also partially relevant by definition.

Collapsing them into a binary label teaches over-confident rejection: the
model learns to push away pages that a human would call relevant, and the
resulting regression is invisible to any metric that only asks "was a relevant
page retrieved". So grade 1 is mined, capped relative to grade 0, and kept out
of the grade 0 bucket in every reported number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.retrieval.types import EvalCase, GoldEvidence

#: Answer-bearing. A positive must be at least this graded.
ANSWER_GRADE = 3

#: Related but not answering. The category that makes mining useful.
RELATED_GRADE = 1


@dataclass(slots=True)
class MiningConfig:
    #: Cap on grade 1 negatives **per positive**. Without a cap the miner fills
    #: the batch with related-but-wrong pages and the model over-rejects.
    max_related_per_positive: int = 4
    #: Cap on grade 0 negatives per positive.
    max_irrelevant_per_positive: int = 2
    #: Retrieval rank range a candidate must fall in to count as hard. A
    #: candidate the retriever ranked 400th is not hard, it is simply bad.
    rank_window: int = 50


@dataclass(slots=True)
class MiningPair:
    """One training example."""

    case_id: UUID
    question: str
    positive_doc_id: str
    positive_page_no: int | None
    positive_grade: int
    negative_doc_id: str
    negative_page_no: int | None
    #: 0 or 1. Recorded per example so downstream mixing ratios are auditable.
    negative_grade: int
    #: Why this negative was chosen — same document, adjacent page, or
    #: high-ranked miss. Kept for diagnosis, not for training.
    negative_reason: str


@dataclass(slots=True)
class MiningResult:
    pairs: list[MiningPair] = field(default_factory=list)
    related_count: int = 0
    irrelevant_count: int = 0
    cases_without_positive: int = 0

    def summary(self) -> dict[str, float | int]:
        total = len(self.pairs)
        return {
            "pairs": total,
            "related_negatives": self.related_count,
            "irrelevant_negatives": self.irrelevant_count,
            "related_ratio": (self.related_count / total) if total else 0.0,
            "cases_without_positive": self.cases_without_positive,
        }


def _reason(candidate: GoldEvidence, positives: list[GoldEvidence]) -> str:
    if any(str(p.doc_id) == str(candidate.doc_id) for p in positives):
        return "same_document"
    for positive in positives:
        if (
            positive.page_no is not None
            and candidate.page_no is not None
            and abs(positive.page_no - candidate.page_no) <= 1
        ):
            return "adjacent_page"
    return "cross_topic"


def mine_case(
    case: EvalCase,
    *,
    config: MiningConfig | None = None,
) -> MiningResult:
    """Mine negatives for one eval case from its graded gold evidence.

    Negatives come from the case's own labels, never from unlabelled traffic. A
    traffic negative has no gold positive to pair against and no graded
    distinction between "related" and "irrelevant" — the two things the miner
    exists to exploit.
    """
    config = config or MiningConfig()
    positives = [g for g in case.gold_evidence if g.grade >= ANSWER_GRADE]
    if not positives:
        return MiningResult(cases_without_positive=1)

    related = [g for g in case.gold_evidence if g.grade == RELATED_GRADE]
    irrelevant = [g for g in case.gold_evidence if g.grade == 0]

    result = MiningResult()
    for positive in positives:
        related_taken = 0
        irrelevant_taken = 0
        for candidate in related:
            if related_taken >= config.max_related_per_positive:
                break
            # Same PDF, different page is a crucial hard-negative category.
            # Skip only the identical evidence unit, never the whole document.
            if (
                str(candidate.doc_id) == str(positive.doc_id)
                and candidate.page_no == positive.page_no
            ):
                continue
            result.pairs.append(
                MiningPair(
                    case_id=case.id,
                    question=case.question,
                    positive_doc_id=str(positive.doc_id),
                    positive_page_no=positive.page_no,
                    positive_grade=positive.grade,
                    negative_doc_id=str(candidate.doc_id),
                    negative_page_no=candidate.page_no,
                    negative_grade=RELATED_GRADE,
                    negative_reason=_reason(candidate, positives),
                )
            )
            related_taken += 1
        for candidate in irrelevant:
            if irrelevant_taken >= config.max_irrelevant_per_positive:
                break
            result.pairs.append(
                MiningPair(
                    case_id=case.id,
                    question=case.question,
                    positive_doc_id=str(positive.doc_id),
                    positive_page_no=positive.page_no,
                    positive_grade=positive.grade,
                    negative_doc_id=str(candidate.doc_id),
                    negative_page_no=candidate.page_no,
                    negative_grade=0,
                    negative_reason="cross_topic",
                )
            )
            irrelevant_taken += 1

    result.related_count = sum(1 for p in result.pairs if p.negative_grade == RELATED_GRADE)
    result.irrelevant_count = sum(1 for p in result.pairs if p.negative_grade == 0)
    return result


def mine(
    cases: list[EvalCase],
    *,
    config: MiningConfig | None = None,
) -> MiningResult:
    config = config or MiningConfig()
    if config.max_related_per_positive < 0 or config.max_irrelevant_per_positive < 0:
        raise ValidationError("mining caps must be non-negative")
    combined = MiningResult()
    for case in cases:
        result = mine_case(case, config=config)
        combined.pairs.extend(result.pairs)
        combined.related_count += result.related_count
        combined.irrelevant_count += result.irrelevant_count
        combined.cases_without_positive += result.cases_without_positive
    return combined


def split_by_grade(pairs: list[MiningPair]) -> tuple[list[MiningPair], list[MiningPair]]:
    """``(related, irrelevant)``.

    Callers must keep these apart. Mixing them into one bucket is the specific
    mistake this module's docstring is about.
    """
    related = [p for p in pairs if p.negative_grade == RELATED_GRADE]
    irrelevant = [p for p in pairs if p.negative_grade == 0]
    return related, irrelevant


def doc_ids_of(pairs: list[MiningPair]) -> set[str]:
    return {p.negative_doc_id for p in pairs}