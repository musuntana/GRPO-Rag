"""Retrieval metrics with explicit denominators.

Architecture §13.3. The claim under test is that a metric without a stated
denominator cannot be regressed — only eyeballed — and that grade 1 must not be
collapsed into grade 0, because that is what makes hard-negative mining useful
rather than harmful.
"""

from __future__ import annotations

import math
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.retrieval.metrics import (
    Grade,
    abstention_accuracy,
    citation_accuracy,
    citation_completeness,
    compare_bundles,
    dcg,
    evaluate_retrieval,
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from ragos.retrieval.types import EvalCase, GoldEvidence


def _gold(*pairs: tuple[str, int]) -> list[GoldEvidence]:
    return [GoldEvidence(doc_id=doc_id, grade=grade) for doc_id, grade in pairs]


def _retrieved(*pairs: tuple[str, int]) -> list[Grade]:
    return [Grade(doc_id=doc_id, grade=grade) for doc_id, grade in pairs]


# -- recall / precision -----------------------------------------------------


def test_recall_measures_whether_the_answer_survived_retrieval() -> None:
    gold = _gold(("doc-18", 3))

    assert recall_at_k(_retrieved(("doc-17", 1), ("doc-18", 3)), gold, 50) == 1.0
    assert recall_at_k(_retrieved(("doc-17", 1)), gold, 50) == 0.0


def test_recall_counts_grade_one_documents_as_positive() -> None:
    """Grade 1 is relevant-but-not-answering; it is still a positive for recall."""
    gold = _gold(("doc-17", 1), ("doc-18", 3))

    assert recall_at_k(_retrieved(("doc-17", 1)), gold, 50) == 0.5


def test_recall_is_zero_when_there_is_no_positive_gold() -> None:
    assert recall_at_k(_retrieved(("doc-1", 0)), _gold(("doc-1", 0)), 10) == 0.0


def test_recall_and_precision_separate_retrieval_from_ranking_failures() -> None:
    """The division of labour the whole two-stage design rests on."""
    gold = _gold(("doc-18", 3))

    # Answer present but ranked last: recall passes, precision at 1 fails.
    deep = _retrieved(("a", 0), ("b", 0), ("doc-18", 3))

    assert recall_at_k(deep, gold, 50) == 1.0
    assert precision_at_k(deep, gold, 1) == 0.0
    assert reciprocal_rank(deep, gold) == pytest.approx(1 / 3)


def test_precision_uses_the_gold_grade_not_the_returned_grade() -> None:
    """A retrieved label is the judge's opinion; the gold label is the truth."""
    gold = _gold(("doc-18", 3))

    assert precision_at_k(_retrieved(("doc-18", 0)), gold, 1) == 1.0


def test_grades_are_keyed_per_document_not_per_chunk() -> None:
    """A page parent returns several children; a chunk key would double-count."""
    gold = _gold(("doc-18", 3))
    retrieved = [Grade("doc-18", 3), Grade("doc-18", 3)]

    assert precision_at_k(retrieved, gold, 2) == 1.0


# -- ranking metrics --------------------------------------------------------


def test_reciprocal_rank_matches_the_documented_example() -> None:
    """Ranks 1, 2, 5 → (1 + 1/2 + 1/5) / 3."""
    at_rank_1 = reciprocal_rank(_retrieved(("doc-18", 3)), _gold(("doc-18", 3)))
    at_rank_2 = reciprocal_rank(
        _retrieved(("x", 0), ("doc-18", 3)), _gold(("doc-18", 3))
    )
    at_rank_5 = reciprocal_rank(
        _retrieved(("a", 0), ("b", 0), ("c", 0), ("d", 0), ("doc-18", 3)),
        _gold(("doc-18", 3)),
    )

    assert (at_rank_1, at_rank_2, at_rank_5) == (1.0, 0.5, pytest.approx(0.2))
    assert (at_rank_1 + at_rank_2 + at_rank_5) / 3 == pytest.approx(0.567, abs=1e-3)


def test_ndcg_rewards_grade_three_above_grade_one() -> None:
    gold = _gold(("doc-18", 3), ("doc-17", 1))

    best = ndcg_at_k(_retrieved(("doc-18", 3), ("doc-17", 1)), gold, 2)
    swapped = ndcg_at_k(_retrieved(("doc-17", 1), ("doc-18", 3)), gold, 2)

    assert best > swapped
    assert best == pytest.approx(1.0)


def test_dcg_uses_log_discount_and_grade_exponent() -> None:
    assert dcg([3]) == pytest.approx(7 / math.log2(2))
    assert dcg([0, 3]) == pytest.approx(7 / math.log2(3))
    assert dcg([0, 0]) == 0.0


def test_hit_at_k_only_counts_answer_bearing_pages() -> None:
    gold = _gold(("doc-17", 1), ("doc-18", 3))

    assert hit_at_k(_retrieved(("doc-17", 1)), gold, 6) == 0.0
    assert hit_at_k(_retrieved(("doc-18", 3)), gold, 6) == 1.0


def test_hit_at_k_is_undefined_without_an_answer_bearing_page() -> None:
    assert hit_at_k(_retrieved(("doc-1", 1)), _gold(("doc-1", 1)), 6) == 0.0


# -- page-anchored evidence identity ---------------------------------------


def test_wrong_page_from_same_document_is_not_a_hit() -> None:
    """A non-answer page from the right PDF must never count as the answer."""
    gold = [GoldEvidence(doc_id="contract", page_no=7, grade=3)]
    wrong = [Grade(doc_id="contract", page_no=8, grade=3)]

    assert hit_at_k(wrong, gold, 6) == 0.0
    assert recall_at_k(wrong, gold, 50) == 0.0
    assert precision_at_k(wrong, gold, 1) == 0.0
    assert reciprocal_rank(wrong, gold) == 0.0
    assert ndcg_at_k(wrong, gold, 10) == 0.0


def test_correct_page_after_wrong_page_has_lower_rank() -> None:
    gold = [GoldEvidence(doc_id="contract", page_no=7, grade=3)]
    ranked = [
        Grade(doc_id="contract", page_no=8, grade=3),
        Grade(doc_id="contract", page_no=7, grade=0),
    ]

    assert hit_at_k(ranked, gold, 1) == 0.0
    assert hit_at_k(ranked, gold, 2) == 1.0
    assert reciprocal_rank(ranked, gold) == 0.5
    assert ndcg_at_k(ranked, gold, 2) < 1.0


def test_two_pages_in_one_document_are_two_recall_targets() -> None:
    gold = [
        GoldEvidence(doc_id="contract", page_no=7, grade=3),
        GoldEvidence(doc_id="contract", page_no=8, grade=1),
    ]
    retrieved = [Grade(doc_id="contract", page_no=8, grade=1)]

    assert recall_at_k(retrieved, gold, 50) == 0.5
    assert hit_at_k(retrieved, gold, 6) == 0.0


def test_unpaged_candidate_cannot_claim_page_anchored_gold() -> None:
    gold = [GoldEvidence(doc_id="contract", page_no=7, grade=3)]
    candidate = [Grade(doc_id="contract", page_no=None, grade=3)]

    assert hit_at_k(candidate, gold, 6) == 0.0
    assert recall_at_k(candidate, gold, 50) == 0.0


# -- attribution metrics ----------------------------------------------------


def test_citation_accuracy_states_its_denominator() -> None:
    assert citation_accuracy(supported=94, generated=100) == pytest.approx(0.94)
    assert citation_accuracy(supported=0, generated=0) == 0.0


def test_citation_completeness_measures_how_much_of_the_answer_is_attributed() -> None:
    assert citation_completeness(cited_claims=3, supported_claims=4) == pytest.approx(0.75)
    assert citation_completeness(cited_claims=0, supported_claims=0) == 0.0


def test_abstention_accuracy_scores_both_slices() -> None:
    # Two answerable (should not abstain), one unanswerable (should abstain).
    correct = abstention_accuracy(
        abstained=[False, False, True],
        answerable=[True, True, False],
    )

    assert correct == 1.0


def test_abstention_accuracy_catches_always_proceeding() -> None:
    accuracy = abstention_accuracy(
        abstained=[False, False, False],
        answerable=[True, True, False],
    )

    assert accuracy == pytest.approx(2 / 3)


def test_abstention_accuracy_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        abstention_accuracy([True], [True, False])


# -- grade validation -------------------------------------------------------


def test_grade_outside_zero_to_three_is_rejected() -> None:
    with pytest.raises(ValidationError, match="0-3"):
        GoldEvidence(doc_id="doc-1", grade=4)


# -- regression gate --------------------------------------------------------


def test_any_regression_blocks_promotion() -> None:
    baseline = {"evidence_hit_rate": 0.80, "citation_accuracy": 0.94}
    candidate = {"evidence_hit_rate": 0.86, "citation_accuracy": 0.90}

    verdict = compare_bundles(baseline, candidate)

    assert not verdict.promotable
    assert any("citation_accuracy" in r for r in verdict.regressions)


def test_coupling_guard_blocks_hit_rate_gained_at_the_cost_of_attribution() -> None:
    """The failure mode that makes hit rate alone a dangerous reward."""
    baseline = {"evidence_hit_rate": 0.80, "citation_accuracy": 0.94}
    candidate = {"evidence_hit_rate": 0.90, "citation_accuracy": 0.80}

    verdict = compare_bundles(baseline, candidate)

    assert not verdict.promotable
    assert any("coupling" in r for r in verdict.regressions)


def test_coupling_guard_also_watches_abstention() -> None:
    baseline = {"evidence_hit_rate": 0.80, "abstention_accuracy": 0.90}
    candidate = {"evidence_hit_rate": 0.90, "abstention_accuracy": 0.70}

    assert not compare_bundles(baseline, candidate).promotable


def test_uniform_improvement_is_promotable() -> None:
    baseline = {"evidence_hit_rate": 0.80, "citation_accuracy": 0.94}
    candidate = {"evidence_hit_rate": 0.86, "citation_accuracy": 0.96}

    verdict = compare_bundles(baseline, candidate)

    assert verdict.promotable
    assert verdict.improvements["evidence_hit_rate"] == pytest.approx(0.06)


def test_lower_is_better_metrics_block_on_increase() -> None:
    verdict = compare_bundles({"latency_ms": 100.0}, {"latency_ms": 140.0})

    assert not verdict.promotable


# -- aggregate --------------------------------------------------------------


def test_evaluate_retrieval_reports_the_full_metric_set() -> None:
    cases = [
        EvalCase(
            id=uuid4(),
            tenant_id=uuid4(),
            question="q",
            gold_evidence=_gold(("doc-18", 3)),
        )
    ]
    retrieved = [_retrieved(("doc-18", 3))]

    metrics = evaluate_retrieval(cases, retrieved).as_dict()

    assert set(metrics) == {
        "recall_at_5",
        "recall_at_10",
        "recall_at_50",
        "precision_at_10",
        "ndcg_at_10",
        "mrr_at_10",
        "evidence_hit_rate",
    }
    assert metrics["evidence_hit_rate"] == 1.0


def test_evaluate_retrieval_rejects_mismatched_lengths() -> None:
    case = EvalCase(id=uuid4(), tenant_id=uuid4(), question="q")

    with pytest.raises(ValueError, match="same length"):
        evaluate_retrieval([case], [])