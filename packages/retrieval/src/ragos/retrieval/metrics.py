"""Retrieval metrics.

Every function lives in this one module so the evaluation harness and the
production gate cannot compute the same name two ways (architecture §13.3).
Definitions use explicit denominators: a metric whose denominator is implicit
cannot be regressed, only eyeballed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from ragos.retrieval.types import EvalCase, GoldEvidence


@dataclass(slots=True)
class Grade:
    """A graded relevance judgment for one retrieved document/page.

    Graded rather than binary because grade 1 — relevant to the topic but not
    containing the answer — is what distinguishes a useful hard negative from a
    harmful one. Collapsing it into 0 removes the hardest signal from both
    training and scoring.
    """

    doc_id: str
    grade: int
    page_no: int | None = None


def _key(grade: Grade | GoldEvidence) -> tuple[str, int | None]:
    """Match evidence by document and page; never infer page from doc ID.

    An unpaged candidate cannot claim a page-anchored gold label. Unpaged
    token-window and legacy cases continue to match with page_no=None.
    """
    return str(grade.doc_id), grade.page_no


def unique_evidence(retrieved: Iterable[Grade]) -> list[Grade]:
    """Keep the first hit for each (document, page), preserving rank.

    Vector search commonly returns several chunks from the same page. Counting
    them as distinct evidence inflates NDCG/precision and wastes Top-K slots.
    """
    seen: set[tuple[str, int | None]] = set()
    result: list[Grade] = []
    for item in retrieved:
        key = _key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def precision_at_k(
    retrieved: Sequence[Grade],
    gold: Iterable[GoldEvidence],
    k: int,
) -> float:
    top = unique_evidence(retrieved)[:k]
    if not top:
        return 0.0
    gold_grades = {_key(g): g.grade for g in gold}
    relevant = sum(1 for c in top if gold_grades.get(_key(c), 0) > 0)
    return relevant / len(top)


def recall_at_k(
    retrieved: Sequence[Grade],
    gold: Iterable[GoldEvidence],
    k: int,
) -> float:
    """Did retrieval surface the gold pages at all.

    ``|gold ∩ retrieved@k| / |gold|`` over evidence units with grade > 0. This is
    the retriever's own objective (recall); without it a recall failure and a
    ranking failure are indistinguishable.
    """
    gold_positive = {_key(g) for g in gold if g.grade > 0}
    if not gold_positive:
        return 0.0
    top_evidence = {_key(c) for c in unique_evidence(retrieved)[:k]}
    return len(gold_positive & top_evidence) / len(gold_positive)


def reciprocal_rank(
    retrieved: Sequence[Grade],
    gold: Iterable[GoldEvidence],
) -> float:
    gold_positive = {_key(g) for g in gold if g.grade > 0}
    for rank, candidate in enumerate(unique_evidence(retrieved), start=1):
        if _key(candidate) in gold_positive:
            return 1.0 / rank
    return 0.0


def mrr(
    retrieved_by_case: Sequence[Sequence[Grade]],
    gold_by_case: Sequence[Sequence[GoldEvidence]],
) -> float:
    if not retrieved_by_case:
        return 0.0
    scores = [
        reciprocal_rank(r, g)
        for r, g in zip(retrieved_by_case, gold_by_case, strict=True)
    ]
    return sum(scores) / len(scores)


def dcg(grades: Sequence[int]) -> float:
    import math

    total = 0.0
    for i, g in enumerate(grades, start=1):
        if g > 0:
            total += (2**g - 1) / math.log2(i + 1)
    return total


def ndcg_at_k(
    retrieved: Sequence[Grade],
    gold: Iterable[GoldEvidence],
    k: int,
) -> float:
    gold_grades = {_key(g): g.grade for g in gold}
    actual = [gold_grades.get(_key(c), 0) for c in unique_evidence(retrieved)[:k]]
    ideal = sorted(gold_grades.values(), reverse=True)[:k]
    idcg = dcg(ideal)
    if idcg <= 0.0:
        return 0.0
    return dcg(actual) / idcg


def hit_at_k(retrieved: Sequence[Grade], gold: Iterable[GoldEvidence], k: int) -> float:
    """Did an answer-bearing (grade 3) page reach the top K?"""
    target = {_key(g) for g in gold if g.grade >= 3}
    if not target:
        return 0.0
    return 1.0 if any(_key(c) in target for c in unique_evidence(retrieved)[:k]) else 0.0


def citation_accuracy(supported: int, generated: int) -> float:
    """``supported_citations / generated_citations``.

    Both counts come from the eval case's ``supported_claims``. A metric without
    a stated denominator is an impression, not a gate.
    """
    if generated <= 0:
        return 0.0
    return supported / generated


def citation_completeness(cited_claims: int, supported_claims: int) -> float:
    """``cited_claims / supported_claims`` — how much of the answer is attributed."""
    if supported_claims <= 0:
        return 0.0
    return cited_claims / supported_claims


def abstention_accuracy(
    abstained: Sequence[bool],
    answerable: Sequence[bool],
) -> float:
    """Accuracy of the refuse decision, over both answerable slices.

    The gate has a first-class refuse branch; without this metric that branch is
    unmeasured.
    """
    if len(abstained) != len(answerable):
        raise ValueError("abstained and answerable must be the same length")
    if not abstained:
        return 0.0
    correct = sum(1 for a, ans in zip(abstained, answerable, strict=True) if a != ans)
    return correct / len(abstained)


@dataclass(slots=True)
class RetrievalMetrics:
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    recall_at_50: float = 0.0
    precision_at_10: float = 0.0
    ndcg_at_10: float = 0.0
    mrr_at_10: float = 0.0
    evidence_hit_rate: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "recall_at_5": self.recall_at_5,
            "recall_at_10": self.recall_at_10,
            "recall_at_50": self.recall_at_50,
            "precision_at_10": self.precision_at_10,
            "ndcg_at_10": self.ndcg_at_10,
            "mrr_at_10": self.mrr_at_10,
            "evidence_hit_rate": self.evidence_hit_rate,
        }


def evaluate_retrieval(
    cases: Sequence[EvalCase],
    retrieved_by_case: Sequence[Sequence[Grade]],
) -> RetrievalMetrics:
    if not cases:
        return RetrievalMetrics()
    if len(cases) != len(retrieved_by_case):
        raise ValueError("cases and retrieved_by_case must be the same length")

    paired = list(zip(cases, retrieved_by_case, strict=True))

    def mean(values: Iterable[float]) -> float:
        vals = list(values)
        return sum(vals) / len(vals) if vals else 0.0

    def per_case(fn, *args) -> float:
        return mean(fn(r, c.gold_evidence, *args) for c, r in paired)

    def mrr_per_case() -> float:
        return mean(
            reciprocal_rank(unique_evidence(r)[:10], c.gold_evidence)
            for c, r in paired
        )

    return RetrievalMetrics(
        recall_at_5=per_case(recall_at_k, 5),
        recall_at_10=per_case(recall_at_k, 10),
        recall_at_50=per_case(recall_at_k, 50),
        precision_at_10=per_case(precision_at_k, 10),
        ndcg_at_10=per_case(ndcg_at_k, 10),
        mrr_at_10=mrr_per_case(),
        evidence_hit_rate=per_case(hit_at_k, 6),
    )


@dataclass(slots=True)
class ComparisonVerdict:
    """Result of the regression gate (architecture §13.3)."""

    promotable: bool
    regressions: list[str] = field(default_factory=list)
    improvements: dict[str, float] = field(default_factory=dict)


#: Metrics where an increase is the goal. A decrease blocks promotion regardless
#: of how much another metric improved.
_HIGHER_IS_BETTER = (
    "precision_at_10",
    "recall_at_5",
    "recall_at_10",
    "recall_at_50",
    "ndcg_at_10",
    "mrr_at_10",
    "evidence_hit_rate",
    "citation_accuracy",
    "citation_completeness",
    "answer_correctness",
    "abstention_accuracy",
    "faithfulness",
)

#: Coupling guard from ADR-0007: a reranker can raise hit rate by promoting
#: confident-but-wrong pages, which improves ordering and degrades attribution
#: at the same time. These pairs block each other.
_COUPLING_PAIRS = (
    ("evidence_hit_rate", "citation_accuracy"),
    ("evidence_hit_rate", "abstention_accuracy"),
)


def compare_bundles(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    epsilon: float = 1e-6,
) -> ComparisonVerdict:
    """Block promotion on any tracked-metric regression.

    "Tracked" is the point: an untracked metric is not evidence of quality, so
    it is not something to trade against a regression.
    """
    regressions: list[str] = []
    improvements: dict[str, float] = {}

    # Missing or nonfinite observations are not evidence for promotion. Check
    # before comparing: NaN makes both ordering comparisons return false.
    if not baseline or not candidate:
        regressions.append("nonempty baseline and candidate metrics are required")
    if set(baseline) != set(candidate):
        regressions.append("baseline and candidate metric sets differ")
    if not all(math.isfinite(value) for value in (*baseline.values(), *candidate.values())):
        regressions.append("metrics must be finite")
    if not math.isfinite(epsilon) or epsilon < 0:
        regressions.append("comparison tolerance must be finite and nonnegative")
    if regressions:
        return ComparisonVerdict(promotable=False, regressions=regressions)

    for key, base_value in baseline.items():
        cand_value = candidate[key]
        if key in _HIGHER_IS_BETTER:
            if cand_value < base_value - epsilon:
                regressions.append(f"{key}: {base_value:.4f} -> {cand_value:.4f}")
            elif cand_value > base_value + epsilon:
                improvements[key] = cand_value - base_value
        else:
            if cand_value > base_value + epsilon:
                regressions.append(f"{key}: {base_value:.4f} -> {cand_value:.4f}")

    for hit_key, attribution_key in _COUPLING_PAIRS:
        if hit_key not in improvements:
            continue
        if attribution_key in baseline and attribution_key in candidate:
            if candidate[attribution_key] < baseline[attribution_key] - epsilon:
                regressions.append(
                    f"coupling: {hit_key} improved but {attribution_key} regressed "
                    f"({baseline[attribution_key]:.4f} -> {candidate[attribution_key]:.4f})"
                )

    return ComparisonVerdict(
        promotable=not regressions,
        regressions=regressions,
        improvements=improvements,
    )


def gold_doc_ids(case: EvalCase, *, min_grade: int = 1) -> set[str]:
    return {str(g.doc_id) for g in case.gold_evidence if g.grade >= min_grade}


def doc_id_of(chunk_doc_id: UUID | str | None) -> str:
    return "" if chunk_doc_id is None else str(chunk_doc_id)
