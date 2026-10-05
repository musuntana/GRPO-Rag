"""Evaluation service: run a bundle against a case set, gate a promotion.

Architecture §13.3. Two rules are enforced here rather than left to callers:

**Split discipline.** ``dev`` drives thresholds, prompts, and hyperparameters.
``holdout`` decides promotion. A run evaluated on the split it tuned on is
reported as invalid, because a metric tuned on holdout reports the tuning.

**One grader, two consumers.** NDCG@k and precision@k are computed from the
same rails prompt the production CRAG gate uses, and the rails prompt is part
of the frozen bundle — otherwise a prompt edit silently invalidates historical
metric comparisons while production keeps running on the new behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.retrieval.grader import RAILS_PROMPT_VERSION, rails_prompt_id
from ragos.retrieval.metrics import (
    Grade,
    abstention_accuracy,
    citation_accuracy,
    citation_completeness,
    compare_bundles,
    evaluate_retrieval,
)
from ragos.retrieval.types import EvalCase, ExperimentBundle

#: The splits an eval run may be scored on.
DEV = "dev"
HOLDOUT = "holdout"


@dataclass(slots=True)
class CaseOutcome:
    """What one case produced.

    Per-step attribution: the retrieval outcome is recorded separately from the
    generation outcome, so a quality regression localizes to the stage that
    caused it instead of showing up as one bad final answer.
    """

    case_id: UUID
    split: str

    retrieved: list[Grade] = field(default_factory=list)
    evidence_hit: bool = False

    answer: str | None = None
    abstained: bool = False
    correct: bool | None = None

    generated_citations: int = 0
    supported_citations: int = 0
    cited_claims: int = 0
    supported_claims: int = 0

    rails_prompt_id: str | None = None


@dataclass(slots=True)
class EvalRunResult:
    bundle_id: str
    bundle: dict[str, str]
    split: str
    case_total: int
    metrics: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "bundle_id": self.bundle_id,
            "bundle": self.bundle,
            "split": self.split,
            "case_total": self.case_total,
            "metrics": self.metrics,
            "status": "completed",
        }


def compute_metrics(outcomes: Sequence[CaseOutcome], cases: Sequence[EvalCase]) -> dict[str, float]:
    """Aggregate metrics with explicit denominators.

    The attribution metrics are computed over the subset of cases that actually
    carry claims or citations. Averaging them over all cases would let a large
    un-cited set dilute the ratio toward 1.0 and make an attribution regression
    look like an improvement.
    """
    if len(outcomes) != len(cases):
        raise ValidationError(
            "outcomes and cases must be the same length",
            outcomes=len(outcomes),
            cases=len(cases),
        )

    retrieval = evaluate_retrieval(
        cases, [o.retrieved for o in outcomes]
    ).as_dict()

    correct_values = [
        o.correct
        for o, c in zip(outcomes, cases, strict=True)
        if o.correct is not None and c.answerable
    ]

    metrics: dict[str, float] = dict(retrieval)

    if correct_values:
        metrics["answer_correctness"] = sum(1 for v in correct_values if v) / len(correct_values)

    abstained = [o.abstained for o in outcomes]
    answerable_flags = [c.answerable for c in cases]
    if any(not a for a in answerable_flags):
        metrics["abstention_accuracy"] = abstention_accuracy(abstained, answerable_flags)

    citation_cases = [o for o in outcomes if o.generated_citations > 0]
    if citation_cases:
        supported = sum(o.supported_citations for o in citation_cases)
        generated = sum(o.generated_citations for o in citation_cases)
        metrics["citation_accuracy"] = citation_accuracy(supported, generated)

    completeness_cases = [o for o in outcomes if o.supported_claims > 0]
    if completeness_cases:
        cited = sum(o.cited_claims for o in completeness_cases)
        total = sum(o.supported_claims for o in completeness_cases)
        metrics["citation_completeness"] = citation_completeness(cited, total)

    return metrics


def run_eval(
    *,
    tenant_scope_key: str,
    bundle: ExperimentBundle,
    cases: Sequence[EvalCase],
    outcomes: Sequence[CaseOutcome],
    split: str = HOLDOUT,
) -> EvalRunResult:
    """Run an eval and return the recorded result.

    Refuses a run whose ``rails_prompt_id`` disagrees with the bundle's — a
    mismatch means the grades came from a different grader than the one the
    bundle claims, which makes every retrieval metric in the result
    incomparable with every other run.
    """
    if not tenant_scope_key:
        raise PermissionDeniedError(
            "eval requires a scope_key; refusing to run unscoped"
        )
    if split not in (DEV, HOLDOUT):
        raise ValidationError("split must be 'dev' or 'holdout'", split=split)

    observed_prompt_ids = {
        o.rails_prompt_id for o in outcomes if o.rails_prompt_id is not None
    }
    if observed_prompt_ids and observed_prompt_ids != {bundle.rails_prompt_version}:
        raise ValidationError(
            "outcome grades came from a different rails prompt than the bundle",
            bundle_rails_prompt=bundle.rails_prompt_version,
            observed=sorted(observed_prompt_ids),
        )

    if not cases:
        return EvalRunResult(
            bundle_id=bundle.bundle_id,
            bundle=bundle.as_dict(),
            split=split,
            case_total=0,
            metrics={},
        )

    metrics = compute_metrics(outcomes, cases)
    return EvalRunResult(
        bundle_id=bundle.bundle_id,
        bundle=bundle.as_dict(),
        split=split,
        case_total=len(cases),
        metrics=metrics,
    )


def split_cases(cases: Sequence[EvalCase], split: str) -> list[EvalCase]:
    return [c for c in cases if c.split == split]


def gate(
    *,
    baseline: EvalRunResult,
    candidate: EvalRunResult,
    tuned_split: str = DEV,
) -> object:
    """Run the regression gate between two bundles.

    A candidate scored on the split it was tuned on is not promotable. That is
    reported as a regression rather than silently passed, because the failure
    mode is a metric that improves forever while meaning nothing.
    """
    from ragos.retrieval.metrics import ComparisonVerdict

    if candidate.split == tuned_split:
        return ComparisonVerdict(
            promotable=False,
            regressions=[
                f"candidate scored on '{candidate.split}', the split it was tuned on; "
                "a train-set improvement is a bug, not a result"
            ],
        )
    return compare_bundles(baseline.metrics, candidate.metrics)


def validate_grader_agreement(
    *,
    rails_prompt_version: str,
    human_grades: Sequence[int],
    grader_grades: Sequence[int],
    min_agreement: float = 0.8,
) -> dict:
    """Check the rails grader against human labels.

    One grader serves both production and evaluation, so a bad rails prompt
    degrades the gate and the metric that justifies the gate simultaneously. A
    grader that has never been checked against humans is an unmeasured
    assumption sitting under every retrieval metric in the design.

    Exact-match agreement is used rather than a correlation measure: the gate
    branches on a threshold, so what matters is whether it lands on the same
    side of that threshold, not whether the grades are ordinally close.
    """
    if len(human_grades) != len(grader_grades):
        raise ValidationError(
            "human and grader grade lists must be the same length",
            human=len(human_grades),
            grader=len(grader_grades),
        )
    if not human_grades:
        return {
            "rails_prompt_version": rails_prompt_version,
            "sample_size": 0,
            "agreement": 0.0,
            "acceptable": False,
            "reason": "no human-labelled sample",
        }

    agree = sum(1 for h, g in zip(human_grades, grader_grades, strict=True) if h == g)
    ratio = agree / len(human_grades)

    confusion: dict[str, int] = {}
    for h, g in zip(human_grades, grader_grades, strict=True):
        confusion[f"{h}->{g}"] = confusion.get(f"{h}->{g}", 0) + 1

    return {
        "rails_prompt_version": rails_prompt_version,
        "sample_size": len(human_grades),
        "agreement": ratio,
        "confusion": confusion,
        "acceptable": ratio >= min_agreement,
    }


def default_bundle(**overrides: str) -> ExperimentBundle:
    fields = {
        "prompt_version": "p1",
        "workflow_version": "w1",
        "model_profile_version": "m1",
        "retriever_profile_version": "r1",
        "parser_version": "pdf-v1",
        "chunker_version": "pdf-v1:page:c400:p1600",
        "memory_profile_version": "mem1",
        "rails_prompt_version": rails_prompt_id(),
    }
    fields.update(overrides)
    return ExperimentBundle(**fields)  # type: ignore[arg-type]


__all__ = [
    "CaseOutcome",
    "DEV",
    "HOLDOUT",
    "RAILS_PROMPT_VERSION",
    "EvalRunResult",
    "compute_metrics",
    "default_bundle",
    "gate",
    "run_eval",
    "split_cases",
    "validate_grader_agreement",
]