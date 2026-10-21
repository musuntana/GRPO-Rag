"""One authorized Dense recall pass, two strictly paired page-ranking traces.

The evaluator supplies a gateway-authorized RerankFn; this operator-only
function never resolves a model, owns credentials or exposes an HTTP endpoint.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any

from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.benchmark import compare_page_traces
from ragos.retrieval.reranker import RerankFn, rerank
from ragos.retrieval.types import EvalCase, RetrieverProfile, ScoredChunk
from ragos.runtime.index_plan import IndexPlan
from ragos.runtime.page_trace_capture import capture_page_ranking_trace

# Hard bounds prevent a private 1,200-case benchmark from retaining unbounded
# source passages. Operators may run several separately frozen batches.
MAX_CACHED_CHUNKS = 20_000
MAX_CACHED_TEXT_CHARS = 32 * 1024 * 1024

_IMMUTABLE = tuple(
    field.name for field in fields(ScoredChunk)
    if field.name not in ("score", "rerank_score")
)


class _FrozenDense:
    """Copy each initial authorized result once; never resubmit model/ranking gold."""

    def __init__(self, dense: Any) -> None:
        self._dense = dense
        self._by_question: dict[str, tuple[ScoredChunk, ...]] = {}
        self._cached_chunks = 0
        self._cached_text_chars = 0

    async def recall(
        self, query: str, tenant_ctx: TenantContext, profile: RetrieverProfile
    ) -> list[ScoredChunk]:
        if query not in self._by_question:
            hits = await self._dense.recall(query, tenant_ctx, profile)
            if not isinstance(hits, list) or any(not isinstance(hit, ScoredChunk) for hit in hits):
                raise ValidationError("dense recall must return ScoredChunk records")
            if any(
                not isinstance(hit.text, str)
                or (hit.source_text is not None and not isinstance(hit.source_text, str))
                for hit in hits
            ):
                raise ValidationError("dense recall returned invalid text fields")
            added_chars = sum(len(hit.text) + len(hit.source_text or "") for hit in hits)
            if (
                self._cached_chunks + len(hits) > MAX_CACHED_CHUNKS
                or self._cached_text_chars + added_chars > MAX_CACHED_TEXT_CHARS
            ):
                raise ValidationError("paired trace candidate cache exceeded the memory budget")
            self._by_question[query] = tuple(replace(hit) for hit in hits)
            self._cached_chunks += len(hits)
            self._cached_text_chars += added_chars
        return [replace(hit) for hit in self._by_question[query]]


class _ScoredRecall:
    """Rerank the top depth, keep the untouched tail in the original order."""

    def __init__(self, frozen: _FrozenDense, model: RerankFn) -> None:
        self._frozen = frozen
        self._model = model

    async def recall(
        self, query: str, tenant_ctx: TenantContext, profile: RetrieverProfile
    ) -> list[ScoredChunk]:
        candidates = await self._frozen.recall(query, tenant_ctx, profile)
        depth = min(profile.top_k_rerank, len(candidates))
        if not depth:
            return candidates
        original_head = {c.chunk_id: replace(c) for c in candidates[:depth]}
        if len(original_head) != depth:
            raise ValidationError("duplicate reranker input chunk identity")
        result = await rerank(query, candidates, profile, tenant_ctx, reranker=self._model)
        if len(result.ranked) != depth:
            raise ValidationError("reranker must return the entire input head")
        reordered: list[ScoredChunk] = []
        seen: set = set()
        for candidate in result.ranked:
            original = original_head.get(candidate.chunk_id)
            if original is None or candidate.chunk_id in seen:
                raise ValidationError("reranker returned unknown or duplicate chunk")
            if any(
                getattr(candidate, field) != getattr(original, field)
                for field in _IMMUTABLE
            ):
                raise ValidationError("reranker modified frozen page evidence")
            if type(candidate.score) not in (int, float) or not math.isfinite(candidate.score):
                raise ValidationError("reranker returned a nonfinite score")
            seen.add(candidate.chunk_id)
            score = float(candidate.score)
            reordered.append(replace(original, score=score, rerank_score=score))
        return reordered + [replace(c) for c in candidates[depth:]]


@dataclass(frozen=True, slots=True)
class PairedPageTraces:
    """Raw JSONL includes private questions and labels; store only in approved locations."""

    baseline: bytes
    candidate: bytes
    comparison: dict[str, Any]


async def capture_paired_page_traces(
    *,
    tenant_ctx: TenantContext,
    plan: IndexPlan,
    dense: Any,
    profile: RetrieverProfile,
    cases: list[EvalCase],
    split: str,
    dataset_id: str,
    baseline_version: str,
    reranker_version: str,
    reranker_fn: RerankFn,
) -> PairedPageTraces:
    """Use ONE real Dense retrieval per question, compare with one reranking pass.

    Scope/approved labels/published index identity are checked by the existing
    capture function *before* model scoring. A model never sees gold labels.
    """
    if split not in ("dev", "holdout"):
        raise ValidationError("paired quality capture does not allow training split")
    if type(profile.top_k_rerank) is not int or profile.top_k_rerank < 1:
        raise ValidationError("paired capture needs a positive rerank depth")
    if not profile.rerank_model_profile or not callable(reranker_fn):
        raise ValidationError("paired capture requires an authorized model reranker")
    if baseline_version == reranker_version:
        raise ValidationError("baseline and reranker versions must differ")
    frozen = _FrozenDense(dense)
    first = await capture_page_ranking_trace(
        tenant_ctx=tenant_ctx, plan=plan, dense=frozen, profile=profile,
        cases=cases, split=split, dataset_id=dataset_id,
        retriever_version=baseline_version,
    )
    second = await capture_page_ranking_trace(
        tenant_ctx=tenant_ctx, plan=plan,
        dense=_ScoredRecall(frozen, reranker_fn), profile=profile,
        cases=cases, split=split, dataset_id=dataset_id,
        retriever_version=reranker_version,
    )
    comparison = compare_page_traces(first, second)
    return PairedPageTraces(baseline=first, candidate=second, comparison=comparison)
