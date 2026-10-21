"""One-recall paired page trace capture, source invariants and leak guards."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.types import EvalCase, GoldEvidence, RetrieverProfile, ScoredChunk
from ragos.runtime.paired_trace_capture import capture_paired_page_traces


def _inputs():
    tenant_id, kb_id, document_id, version_id, representation_id, index_id = (
        uuid4() for _ in range(6)
    )
    ctx = TenantContext(
        tenant_id=tenant_id, tenant_slug="fixture", actor_id="auditor",
        trace_id="paired-capture", allowed_kb_ids=[kb_id],
    )
    plan = SimpleNamespace(
        tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
        document_version_id=version_id, representation_id=representation_id,
        index_id=index_id, page_count=3, embedding_profile="test-embed",
        embedding_version="frozen-test-identity",
    )
    case = EvalCase(
        id=uuid4(), tenant_id=tenant_id, kb_id=kb_id,
        question="What warranty is provided?",
        gold_evidence=[
            GoldEvidence(doc_id=document_id, page_no=2, grade=3),
            GoldEvidence(doc_id=document_id, page_no=1, grade=1),
        ],
        review_state="approved", split="dev",
    )
    profile = RetrieverProfile(
        id=uuid4(), tenant_id=tenant_id, recall_channels=["dense"],
        channel_budgets={"dense": 1.0}, rerank_model_profile="approved-local-test",
        top_k_recall=50, top_k_rerank=3,
    )
    chunks = []
    for page in (1, 3, 2):
        cid = uuid4()
        text = f"frozen page {page}"
        identity = EvidenceIdentity(
            tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
            document_version_id=version_id, representation_id=representation_id,
            index_id=index_id, chunk_id=cid, representation_char_count=100,
            parent_unit="page", page_count=3,
        )
        chunks.append(ScoredChunk(
            chunk_id=cid, doc_id=document_id, kb_id=kb_id,
            text=text, source_text=text, score=0.1, channel="dense",
            page_no=page, page_count=3, source_identity=identity,
        ))
    return ctx, plan, case, profile, chunks


class _Dense:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    async def recall(self, query, tenant_ctx, profile):
        self.calls += 1
        return list(self.chunks)


def _run(ctx, plan, case, profile, dense, fn, *, split="dev"):
    return asyncio.run(capture_paired_page_traces(
        tenant_ctx=ctx, plan=plan, dense=dense, profile=profile,
        cases=[case], split=split, dataset_id="synthetic-reviewed-test-only",
        baseline_version="frozen-dense", reranker_version="model-rerank-v1",
        reranker_fn=fn,
    ))


def test_captures_exactly_one_dense_pass_and_matching_source_universe():
    ctx, plan, case, profile, chunks = _inputs()
    dense = _Dense(chunks)
    calls = []

    async def scored(query, candidates, tenant_ctx):
        calls.append((query, tenant_ctx.tenant_id))
        return [replace(chunk, score=0.95 - index / 10)
                for index, chunk in enumerate(reversed(candidates))]

    result = _run(ctx, plan, case, profile, dense, scored)
    assert dense.calls == 1
    assert calls == [(case.question, ctx.tenant_id)]
    first = json.loads(result.baseline.splitlines()[1])["retrieved"]
    second = json.loads(result.candidate.splitlines()[1])["retrieved"]
    assert [item["page_no"] for item in first] == [1, 3, 2]
    assert [item["page_no"] for item in second] == [2, 3, 1]
    assert result.comparison["delta_candidate_minus_baseline"]["ndcg_at_10"] > 0
    assert result.comparison["case_total"] == 1
    assert "warranty" not in json.dumps(result.comparison)


@pytest.mark.parametrize("tamper", ["missing", "duplicate", "text", "in_place"])
def test_pair_rejects_invalid_or_mutated_scoring_outputs(tamper: str) -> None:
    ctx, plan, case, profile, chunks = _inputs()
    dense = _Dense(chunks)

    async def scored(query, candidates, tenant_ctx):
        if tamper == "missing":
            return candidates[:-1]
        if tamper == "duplicate":
            return [candidates[0], candidates[0], candidates[2]]
        if tamper == "text":
            return [replace(candidates[0], text="malicious")]+candidates[1:]
        candidates[0].text = "mutated source in place"
        return candidates

    with pytest.raises(ValidationError):
        _run(ctx, plan, case, profile, dense, scored)
    assert dense.calls == 1


def test_approval_and_train_rejected_before_any_rerank() -> None:
    ctx, plan, case, profile, chunks = _inputs()
    dense = _Dense(chunks)
    invoked = False

    async def scored(query, candidates, tenant_ctx):
        nonlocal invoked
        invoked = True
        return candidates

    with pytest.raises(ValidationError, match="approved"):
        _run(ctx, plan, replace(case, review_state="pending"), profile, dense, scored)
    assert dense.calls == 0
    assert invoked is False
    with pytest.raises(ValidationError, match="training split"):
        _run(ctx, plan, replace(case, split="train"), profile, dense, scored,
             split="train")
    assert dense.calls == 0
    assert invoked is False


def test_profile_must_have_explicit_rerank_model() -> None:
    ctx, plan, case, profile, chunks = _inputs()
    dense = _Dense(chunks)

    async def scored(query, candidates, tenant_ctx):
        return candidates

    with pytest.raises(ValidationError, match="authorized model reranker"):
        _run(ctx, plan, case, replace(profile, rerank_model_profile=None), dense, scored)
    assert dense.calls == 0
