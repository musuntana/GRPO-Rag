"""Fail-closed evidence lineage contract for model-backed reranking."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.reranker import rerank
from ragos.retrieval.types import RetrieverProfile, ScoredChunk


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=uuid4(), tenant_slug="test", actor_id="operator", trace_id="rerank-test"
    )


def _profile(ctx: TenantContext) -> RetrieverProfile:
    return RetrieverProfile(
        id=uuid4(), tenant_id=ctx.tenant_id, rerank_model_profile="approved-reranker"
    )


def _chunks() -> list[ScoredChunk]:
    kb_id = uuid4()
    return [
        ScoredChunk(
            chunk_id=uuid4(), doc_id=uuid4(), kb_id=kb_id,
            text=f"trusted page {i}", score=0.5, channel="dense", layer="L1",
            page_no=i, page_count=2, start_char_idx=0, end_char_idx=14,
            element_type="text", source_text=f"trusted page {i}",
        )
        for i in (1, 2)
    ]


def _run(chunks, fn):
    ctx = _context()
    return asyncio.run(rerank("question", chunks, _profile(ctx), ctx, reranker=fn))


def test_valid_reordering_changes_scores_but_preserves_evidence_metadata() -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        return [
            replace(candidates[1], score=0.9),
            replace(candidates[0], score=0.1),
        ]

    result = _run(chunks, model)
    assert [c.chunk_id for c in result.ranked] == [
        chunks[1].chunk_id, chunks[0].chunk_id,
    ]
    assert [c.rerank_score for c in result.ranked] == [0.9, 0.1]
    assert result.depth == 2
    assert result.rerank_model_profile == "approved-reranker"
    assert result.ranked[0].source_text == "trusted page 2"
    assert chunks[1].rerank_score is None


@pytest.mark.parametrize("failure", ["missing", "duplicate", "foreign"])
def test_rerank_must_be_an_exact_permutation(failure: str) -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        if failure == "missing":
            return [candidates[0]]
        if failure == "duplicate":
            return [candidates[0], candidates[0]]
        return [candidates[0], replace(candidates[1], chunk_id=uuid4())]

    with pytest.raises(ValidationError, match="omitted|duplicated|outside"):
        _run(chunks, model)


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "injected text"),
        ("source_text", "injected source"),
        ("channel", "memory"),
        ("layer", "L3"),
        ("page_no", 19),
        ("page_count", 20),
        ("start_char_idx", 2),
        ("end_char_idx", 99),
        ("parent_chunk_id", uuid4()),
        ("element_type", "table"),
        ("doc_id", uuid4()),
        ("kb_id", uuid4()),
    ],
)
def test_reranker_cannot_change_any_evidence_metadata(field: str, value) -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        changed = replace(candidates[0], **{field: value})
        return [changed, candidates[1]]

    with pytest.raises(ValidationError, match="immutable evidence lineage"):
        _run(chunks, model)


def test_reranker_cannot_mutate_passed_input_objects() -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        candidates[0].text = "tampered in place"
        return candidates

    with pytest.raises(ValidationError, match="immutable evidence lineage"):
        _run(chunks, model)
    # A rejected provider cannot mutate the caller's evidence objects.
    assert chunks[0].text == "trusted page 1"
    assert chunks[0].source_text == "trusted page 1"


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf"), True, "1.0"])
def test_invalid_scores_fail_closed(score) -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        return [replace(candidates[0], score=score), candidates[1]]

    with pytest.raises(ValidationError, match="finite number"):
        _run(chunks, model)


def test_invalid_result_type_fails_closed() -> None:
    chunks = _chunks()

    async def model(_query, _candidates, _ctx):
        return None

    with pytest.raises(ValidationError, match="list"):
        _run(chunks, model)


def test_empty_candidate_set_does_not_call_model() -> None:
    invoked = False

    async def model(_query, _candidates, _ctx):
        nonlocal invoked
        invoked = True
        return []

    result = _run([], model)
    assert result.ranked == []
    assert result.rerank_model_profile is None
    assert invoked is False


def test_successful_rerank_cannot_mutate_original_candidates() -> None:
    chunks = _chunks()

    async def model(_query, candidates, _ctx):
        candidates[0].score = 0.12
        return [candidates[1], candidates[0]]

    result = _run(chunks, model)
    assert result.ranked[1].score == 0.12
    assert [chunk.score for chunk in chunks] == [0.5, 0.5]
    assert all(chunk.rerank_score is None for chunk in chunks)
