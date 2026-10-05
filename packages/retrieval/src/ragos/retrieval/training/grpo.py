"""GRPO-style outcome-reward training for the reranker (ADR-0007 stage 3).

This is the **experiment**, not the deliverable. The distilled cross-encoder
baseline in ``sft.py`` is what ships; this exists to be run against it and
adopted only if it wins on the tenant's holdout split. If it does not win, the
baseline ships and that is a successful outcome of ADR-0007, not a failure of it.

Reward:

    R = alpha * hit_at_k + beta * ndcg_at_k + gamma * hard_negative_demotion

computed against **frozen gold labels**, never against the model's own output.
A reranker that ranks what it was rewarded for ranking is not evidence of
anything, so self-reward is not merely discouraged here — it has no code path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ragos.retrieval.metrics import Grade, hit_at_k, ndcg_at_k
from ragos.retrieval.training.mine import RELATED_GRADE, MiningPair
from ragos.retrieval.types import EvalCase, GoldEvidence


@dataclass(slots=True)
class RewardWeights:
    alpha: float = 0.4
    beta: float = 0.4
    gamma: float = 0.2

    def validate(self) -> None:
        if min(self.alpha, self.beta, self.gamma) < 0:
            raise ValueError("reward weights must be non-negative")
        total = self.alpha + self.beta + self.gamma
        if total <= 0:
            raise ValueError("reward weights must not all be zero")

    @property
    def normalized(self) -> RewardWeights:
        total = self.alpha + self.beta + self.gamma
        return RewardWeights(self.alpha / total, self.beta / total, self.gamma / total)


@dataclass(slots=True)
class Ranking:
    """One sampled ranking for one query."""

    case_id: str
    doc_order: list[str]


@dataclass(slots=True)
class RewardBreakdown:
    hit: float
    ndcg: float
    hard_negative_demotion: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "hit": self.hit,
            "ndcg": self.ndcg,
            "hard_negative_demotion": self.hard_negative_demotion,
            "total": self.total,
        }


def _grades_from_order(order: Sequence[str], gold: Sequence[GoldEvidence]) -> list[Grade]:
    gold_map = {str(g.doc_id): g.grade for g in gold}
    pages = {str(g.doc_id): g.page_no for g in gold}
    return [
        Grade(doc_id=doc_id, grade=gold_map.get(doc_id, 0), page_no=pages.get(doc_id))
        for doc_id in order
    ]


def hard_negative_demotion(
    order: Sequence[str],
    negatives: Sequence[str],
) -> float:
    """Fraction of mined negatives ranked below the top answer-bearing page.

    Grade 1 negatives are the ones this measures against. A model that promotes
    a related-but-not-answering page above the answer has demoted the answer
    even though a naive "is a relevant page present" check still passes.
    """
    if not negatives:
        return 1.0
    negative_set = set(negatives)
    first_negative: int | None = None
    for idx, doc_id in enumerate(order):
        if doc_id in negative_set:
            first_negative = idx
            break
    if first_negative is None:
        return 1.0
    return first_negative / max(len(order), 1)


def compute_reward(
    ranking: Ranking,
    case: EvalCase,
    pairs: Sequence[MiningPair],
    weights: RewardWeights | None = None,
    *,
    k: int = 6,
) -> RewardBreakdown:
    """Reward one ranking against frozen gold labels."""
    weights = (weights or RewardWeights()).normalized
    grades = _grades_from_order(ranking.doc_order, case.gold_evidence)

    hit = hit_at_k(grades, case.gold_evidence, k)
    ndcg = ndcg_at_k(grades, case.gold_evidence, k)
    negatives = [p.negative_doc_id for p in pairs if p.negative_grade == RELATED_GRADE]
    demotion = hard_negative_demotion(ranking.doc_order, negatives)

    total = weights.alpha * hit + weights.beta * ndcg + weights.gamma * demotion
    return RewardBreakdown(hit=hit, ndcg=ndcg, hard_negative_demotion=demotion, total=total)


def group_relative_advantages(rewards: Sequence[float]) -> list[float]:
    """``A_i = (r_i - mean(r)) / (std(r) + eps)``.

    Group-relative rather than absolute: a query whose passages are all weak and
    a query whose passages are all strong should not be compared on the same
    scale, and a value model trained across both would have to. The epsilon
    keeps a group of identical rewards from dividing by zero — a group where
    every sample is equally good carries no ranking signal.
    """
    if not rewards:
        return []
    mean = sum(rewards) / len(rewards)
    var = sum((r - mean) ** 2 for r in rewards) / len(rewards)
    std = var**0.5
    eps = 1e-6
    return [(r - mean) / (std + eps) for r in rewards]


@dataclass(slots=True)
class GroupBatch:
    """A group of sampled rankings for one query."""

    case: EvalCase
    rankings: list[Ranking]
    pairs: list[MiningPair] = field(default_factory=list)
    weights: RewardWeights = field(default_factory=RewardWeights)

    def rewards(self, *, k: int = 6) -> list[RewardBreakdown]:
        return [
            compute_reward(r, self.case, self.pairs, self.weights, k=k)
            for r in self.rankings
        ]

    def advantages(self, *, k: int = 6) -> list[float]:
        return group_relative_advantages([r.total for r in self.rewards(k=k)])


def kl_penalty(
    logprobs: Sequence[float],
    reference_logprobs: Sequence[float],
    *,
    beta: float = 0.01,
) -> float:
    """Mean token-level KL to the SFT reference.

    Keeps the reward-optimized policy near the baseline rather than free to
    satisfy the reward by drifting somewhere the baseline was never trained.
    """
    if not logprobs:
        return 0.0
    total = 0.0
    for lp, ref in zip(logprobs, reference_logprobs, strict=True):
        total += lp - ref
    return beta * (total / len(logprobs))


@dataclass(slots=True)
class PromotionDecision:
    """Whether a candidate artifact may be promoted.

    Four guards, each targeting one way the proxy diverges from the goal.
    """

    promotable: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_promotion(
    *,
    baseline_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    baseline_method: str = "sft",
    candidate_method: str = "grpo",
) -> PromotionDecision:
    """Decide whether a trained artifact may ship.

    GRPO must beat the SFT baseline to be adopted at all. Ties go to the
    baseline: the simpler, cheaper, already-shipped artifact.
    """
    from ragos.retrieval.metrics import compare_bundles

    reasons: list[str] = []

    if candidate_method == "grpo" and baseline_method == "sft":
        primary = candidate_metrics.get("evidence_hit_rate", 0.0)
        base_primary = baseline_metrics.get("evidence_hit_rate", 0.0)
        if primary <= base_primary:
            reasons.append(
                f"grpo did not beat the sft baseline on evidence_hit_rate "
                f"({base_primary:.4f} -> {primary:.4f}); baseline ships"
            )

    verdict = compare_bundles(baseline_metrics, candidate_metrics)
    reasons.extend(verdict.regressions)

    return PromotionDecision(promotable=not reasons, reasons=reasons)