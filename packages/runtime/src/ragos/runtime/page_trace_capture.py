"""Capture page-ranking traces from *authorized, PG-hydrated* dense recall.

This is a developer/evaluation seam, not a user-facing API. A single frozen
document index is accepted per trace manifest. Gold labels cannot be supplied
to the retriever; only the question and trusted tenant context are passed in.
No model answer, private document body or provider credentials are serialized.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import UUID

from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.offline_eval import (
    MAX_CASES,
    MAX_GOLD,
    MAX_RANKED,
    MAX_TRACE_BYTES,
    evaluate_trace_bytes,
)
from ragos.retrieval.recall.dense import DenseRecall
from ragos.retrieval.types import EvalCase, RetrieverProfile, ScoredChunk
from ragos.runtime.index_plan import IndexPlan

REVIEWED = "approved"


def _nonnull(value: str, label: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"{label} must be a nonempty frozen identifier")
    return value


def _reviewed_case(
    case: EvalCase, tenant_ctx: TenantContext, plan: IndexPlan, split: str
) -> None:
    if case.tenant_id != tenant_ctx.tenant_id or case.kb_id != plan.kb_id:
        raise PermissionDeniedError("evaluation case is outside the authorized tenant/KB")
    if case.split != split or case.review_state != REVIEWED or not case.answerable:
        raise ValidationError("page trace requires approved answerable cases on one split")
    if not isinstance(case.question, str) or not case.question.strip():
        raise ValidationError("evaluation question cannot be empty")
    if len(case.question) > 4000 or not 1 <= len(case.gold_evidence) <= MAX_GOLD:
        raise ValidationError("case exceeds frozen evaluation limits")
    if not any(g.grade == 3 for g in case.gold_evidence):
        raise ValidationError("answerable case requires a grade-3 gold page")
    seen: set[tuple[str, int]] = set()
    for gold in case.gold_evidence:
        if gold.page_no is None or type(gold.page_no) is not int:
            raise ValidationError("gold evidence must identify a real PDF page")
        if gold.page_no < 1 or gold.page_no > plan.page_count:
            raise ValidationError("gold evidence is outside frozen PDF page bounds")
        if str(gold.doc_id) != str(plan.document_id):
            raise ValidationError("gold evidence belongs to a different frozen document")
        key = (str(gold.doc_id), gold.page_no)
        if key in seen:
            raise ValidationError("duplicate human-labelled gold page")
        seen.add(key)


def _verified_ranked_pages(
    hits: Sequence[ScoredChunk], tenant_ctx: TenantContext, plan: IndexPlan
) -> list[dict[str, str | int]]:
    if len(hits) > MAX_RANKED:
        raise ValidationError("retrieval returned more than 500 ranked chunks")
    ranked: list[dict[str, str | int]] = []
    for hit in hits:
        identity = hit.require_source_identity()
        if (
            identity.tenant_id != tenant_ctx.tenant_id
            or identity.kb_id != plan.kb_id
            or identity.document_id != plan.document_id
            or identity.document_version_id != plan.document_version_id
            or identity.representation_id != plan.representation_id
            or identity.index_id != plan.index_id
            or identity.parent_unit != "page"
            or identity.page_count != plan.page_count
            or hit.chunk_id != identity.chunk_id
            or hit.doc_id != identity.document_id
            or hit.kb_id != identity.kb_id
            or hit.page_no is None
            or type(hit.page_no) is not int
            or not 1 <= hit.page_no <= identity.page_count
            or not hit.text
            or hit.source_text != hit.text
        ):
            raise ValidationError("ranked evidence is not the frozen published PDF source")
        ranked.append({"doc_id": str(identity.document_id), "page_no": hit.page_no})
    return ranked


def _json_line(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def capture_page_ranking_trace(
    *,
    tenant_ctx: TenantContext,
    plan: IndexPlan,
    dense: DenseRecall,
    profile: RetrieverProfile,
    cases: Sequence[EvalCase],
    split: str,
    dataset_id: str,
    retriever_version: str,
) -> bytes:
    """Record the real ordered, hydrated pages without scoring on gold labels.

    Only the question is fed to `DenseRecall.recall`; the gold list is used
    when serializing the case *after* retrieval has succeeded. An empty
    ranking is a legitimate outcome. Fail on mixed tenants, stale index
    identities, missing page/source text, split drift or unreviewed labels.
    """
    if plan.tenant_id != tenant_ctx.tenant_id or plan.kb_id is None:
        raise PermissionDeniedError("frozen index belongs to a different tenant")
    tenant_ctx.require_private_scope_key()
    if tenant_ctx.allowed_kb_ids is None or plan.kb_id not in tenant_ctx.allowed_kb_ids:
        raise PermissionDeniedError("page trace requires an explicitly authorized KB")
    if profile.tenant_id != tenant_ctx.tenant_id:
        raise PermissionDeniedError("retriever profile belongs to another tenant")
    if split not in ("train", "dev", "holdout"):
        raise ValidationError("split must be train, dev or holdout")
    if not 1 <= len(cases) <= MAX_CASES or profile.top_k_recall > MAX_RANKED:
        raise ValidationError("page trace exceeds frozen case/rank limits")
    _nonnull(dataset_id, "dataset_id")
    _nonnull(retriever_version, "retriever_version")
    _nonnull(plan.embedding_profile, "embedding_profile")
    _nonnull(plan.embedding_version, "embedding_version")
    seen_cases: set[UUID] = set()
    for case in cases:
        if case.id in seen_cases:
            raise ValidationError("evaluation case IDs must be unique")
        seen_cases.add(case.id)
        _reviewed_case(case, tenant_ctx, plan, split)

    manifest = {
        "type": "manifest",
        "schema_version": 2 if split == "train" else 1,
        "dataset_id": dataset_id,
        "tenant_id": str(tenant_ctx.tenant_id),
        "split": split,
        "index_id": str(plan.index_id),
        "embedding_profile": plan.embedding_profile,
        "embedding_version": plan.embedding_version,
        "retriever_version": retriever_version,
    }
    lines = [_json_line(manifest)]
    for case in cases:
        # NEVER pass gold pages or the whole EvalCase into a retriever.
        hits = await dense.recall(case.question, tenant_ctx, profile)
        ranked = _verified_ranked_pages(hits, tenant_ctx, plan)
        lines.append(
            _json_line(
                {
                    "type": "case",
                    "case_id": str(case.id),
                    "split": split,
                    "question": case.question,
                    "gold": [
                        {
                            "doc_id": str(g.doc_id),
                            "page_no": g.page_no,
                            "grade": g.grade,
                        }
                        for g in case.gold_evidence
                    ],
                    "retrieved": ranked,
                }
            )
        )
        if sum(len(line.encode("utf-8")) + 1 for line in lines) > MAX_TRACE_BYTES:
            raise ValidationError("page trace exceeds frozen byte limit")

    raw = ("\n".join(lines) + "\n").encode("utf-8")
    # Make the capture directly consumable by the existing evaluator. This is
    # validation only: no score is used to influence the retrieval operation.
    try:
        evaluate_trace_bytes(raw)
    except ValueError:
        raise ValidationError("captured trace does not satisfy frozen eval schema") from None
    return raw


__all__ = ["capture_page_ranking_trace"]
