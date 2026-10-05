"""Artifact promotion (ADR-0007 stage 4).

An artifact is registered under its own tenant's ``scope_key`` namespace and
served through the gateway like any other ``model_profile``. There is no
cross-tenant artifact path — adding one is a data-governance decision and gets
its own ADR.

Promotion is gated. A trained reranker that improves ordering while degrading
citation accuracy has made the product worse while making the metric look
better, which is the specific failure the coupling guard in
``compare_bundles`` exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import PermissionDeniedError
from ragos.retrieval.training.grpo import PromotionDecision, evaluate_promotion
from ragos.retrieval.training.sft import TrainingRun


@dataclass(slots=True)
class PromotionRequest:
    tenant_id: UUID
    scope_key: str
    run: TrainingRun
    eval_bundle_id: str
    eval_metrics: dict[str, float]
    eval_split: str = "holdout"
    dataset_split: str = "dev"


@dataclass(slots=True)
class PromotionOutcome:
    promoted: bool
    reasons: list[str]
    artifact_ref: str | None = None
    method: str = "sft"


def artifact_path(scope_key: str, version: int) -> str:
    """``{scope_key}/models/reranker/{version}/``.

    Tenant-scoped by construction. A path built from a caller-supplied tenant id
    would let one tenant's promotion write into another's namespace.
    """
    if not scope_key:
        raise PermissionDeniedError(
            "artifact promotion requires a scope_key; refusing to write unscoped"
        )
    return f"{scope_key}/models/reranker/{version}/"


def next_version(existing_versions: list[int]) -> int:
    return (max(existing_versions) + 1) if existing_versions else 1


def promote(
    request: PromotionRequest,
    *,
    baseline_metrics: dict[str, float] | None = None,
    baseline_method: str = "sft",
) -> PromotionOutcome:
    """Gate an artifact and return the promotion outcome.

    Guards, in order:

    1. ``scope_key`` must be present and belong to this tenant.
    2. Split discipline — a run evaluated on the split it tuned on is not
       evidence. It is reported as a failed run, not a weak result.
    3. The cross-metric coupling guard.
    4. For GRPO specifically, it must beat the SFT baseline.
    """
    reasons: list[str] = []

    if not request.scope_key:
        reasons.append("missing scope_key; failing closed rather than writing unscoped")
        return PromotionOutcome(promoted=False, reasons=reasons)

    if request.scope_key.split(":", 1)[0] != str(request.tenant_id):
        reasons.append("scope_key does not belong to this tenant")
        return PromotionOutcome(promoted=False, reasons=reasons)

    if request.eval_split == request.dataset_split:
        reasons.append(
            f"evaluated on the '{request.eval_split}' split it was tuned on; "
            "a train-set improvement is a bug, not a result"
        )
        return PromotionOutcome(promoted=False, reasons=reasons)

    if baseline_metrics is not None:
        decision: PromotionDecision = evaluate_promotion(
            baseline_metrics=baseline_metrics,
            candidate_metrics=request.eval_metrics,
            baseline_method=baseline_method,
            candidate_method=request.run.method,
        )
        if not decision.promotable:
            reasons.extend(decision.reasons)
            return PromotionOutcome(promoted=False, reasons=reasons, method=request.run.method)

    version = next_version([])
    return PromotionOutcome(
        promoted=True,
        reasons=[],
        artifact_ref=artifact_path(request.scope_key, version),
        method=request.run.method,
    )


def register_as_model_profile(
    outcome: PromotionOutcome,
    *,
    profile_name: str,
    tenant_id: UUID,
) -> dict:
    """The ``model_profile`` row a trained artifact becomes.

    ``use_case="rerank"`` and an ``artifact_ref`` — not a special-cased model
    type. Routing stays uniform, so the gateway, key pool, quota, and cost
    accounting apply unchanged and there is no bypass path.
    """
    if not outcome.promoted:
        raise PermissionDeniedError(
            "refusing to register an artifact that was not promoted",
            reasons=outcome.reasons,
        )
    return {
        "tenant_id": str(tenant_id),
        "name": profile_name,
        "use_case": "rerank",
        "artifact_ref": outcome.artifact_ref,
        "is_active": True,
    }


def stale_artifact_reason(
    *,
    artifact_dataset_digest: str,
    current_dataset_digest: str,
    artifact_promoted_at: str | None,
    max_age_days: int = 90,
) -> str | None:
    """Why an artifact should be retrained, or ``None``.

    Artifact staleness is the operational cost of per-tenant models: the corpus
    moves beneath a trained ranker and it degrades silently. An explicit policy
    beats discovering it from a metric drift nobody attributed.
    """
    if artifact_dataset_digest != current_dataset_digest:
        return "training dataset changed since promotion"
    if artifact_promoted_at is None:
        return "artifact has no promotion timestamp"
    from datetime import UTC, datetime, timedelta

    promoted = datetime.fromisoformat(artifact_promoted_at)
    if promoted.tzinfo is None:
        promoted = promoted.replace(tzinfo=UTC)
    if datetime.now(UTC) - promoted > timedelta(days=max_age_days):
        return f"artifact older than {max_age_days} days without re-evaluation"
    return None