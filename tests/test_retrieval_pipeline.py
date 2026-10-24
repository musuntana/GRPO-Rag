"""Retrieval pipeline: fusion, budgets, expansion, gate routing, eval splits.

Architecture §8. The claims under test are that a channel's budget is not
redistributed when it returns nothing, that the majority-sibling merge stops a
parent being emitted repeatedly, and that all three CRAG paths produce the same
shape of result.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import UUID, uuid4, uuid5

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import CitationStatus, EvidenceSet
from ragos.retrieval.eval_svc import (
    CaseOutcome,
    default_bundle,
    gate,
    run_eval,
    split_cases,
    validate_grader_agreement,
)
from ragos.retrieval.expander import (
    dedupe_overlapping_parents,
    expand_to_parents,
    majority_sibling_parents,
)
from ragos.retrieval.fusion import Fusioner, allocate_by_budget, validate_budgets
from ragos.retrieval.grader import CragThresholds, GateAction, RelevanceGrader
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.pipeline import RAGPipeline, RAGResult
from ragos.retrieval.reranker import select_evidence
from ragos.retrieval.types import (
    EvalCase,
    FusionConfig,
    GoldEvidence,
    RetrieverProfile,
    ScoredChunk,
)
from ragos.retrieval.verify import SemanticVerdict

BUDGETS = {"lexical": 0.20, "dense": 0.45, "memory": 0.15, "hot": 0.20}


SYNTHETIC_TENANT_ID = UUID("00000000-0000-0000-0000-000000000101")


def _synthetic_candidate(**fields) -> ScoredChunk:
    """Explicit synthetic immutable lineage for unit tests, never production data."""
    doc = fields.get("doc_id") or uuid4()
    kb = fields.get("kb_id") or UUID("00000000-0000-0000-0000-000000000102")
    fields.update(doc_id=doc, kb_id=kb)
    start = fields.get("start_char_idx") or 0
    fields.setdefault("start_char_idx", start)
    fields.setdefault("end_char_idx", start + len(fields["text"]))
    page = fields.get("page_no")
    count = fields.get("page_count") or 20
    fields["page_count"] = count if page is not None else None
    fields["source_identity"] = EvidenceIdentity(
        tenant_id=SYNTHETIC_TENANT_ID,
        kb_id=kb,
        document_id=doc,
        document_version_id=uuid5(doc, "synthetic-content"),
        representation_id=uuid5(doc, "synthetic-representation"),
        index_id=uuid5(doc, "synthetic-index"),
        chunk_id=fields["chunk_id"],
        representation_char_count=100000,
        parent_unit="page" if page is not None else "token_window",
        page_count=count if page is not None else None,
    )
    fields["source_text"] = fields["text"]
    return ScoredChunk(**fields)


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=SYNTHETIC_TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-1",
    )


def _chunk(
    text: str,
    *,
    channel: str = "dense",
    score: float = 0.9,
    page: int | None = 18,
    doc: UUID | None = None,
    parent: UUID | None = None,
) -> ScoredChunk:
    return _synthetic_candidate(
        chunk_id=uuid4(),
        doc_id=doc or uuid4(),
        kb_id=uuid4(),
        text=text,
        score=score,
        channel=channel,
        page_no=page,
        page_count=20,
        start_char_idx=0,
        end_char_idx=len(text),
        parent_chunk_id=parent,
    )


def _profile(**overrides) -> RetrieverProfile:
    fields = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "recall_channels": ["dense"],
        "channel_budgets": dict(BUDGETS),
    }
    fields.update(overrides)
    profile = RetrieverProfile(**fields)
    return profile


# -- budgets ----------------------------------------------------------------


def test_budgets_must_sum_to_at_most_one() -> None:
    with pytest.raises(ValueError, match="<= 1.0"):
        validate_budgets({"lexical": 0.6, "dense": 0.6})


def test_an_empty_channel_does_not_donate_its_budget() -> None:
    """The rule that per-channel budgets exist to enforce.

    Silently backfilling an absent channel re-introduces the dominance problem:
    dense recall would absorb the whole context on every query that happened to
    have no lexical signal.
    """
    channels = {"dense": [_chunk("a"), _chunk("b")], "lexical": []}

    trimmed, allocations = allocate_by_budget(channels, BUDGETS, total_token_budget=1000)

    lexical = next(a for a in allocations if a.channel == "lexical")
    assert lexical.budget == BUDGETS["lexical"]
    assert lexical.returned == 0
    assert trimmed["lexical"] == []
    # Dense keeps only its own share, not lexical's unused one.
    assert len(trimmed["dense"]) == 2


def test_a_channel_with_a_budget_always_keeps_at_least_one_chunk() -> None:
    """A budget rounding to zero items makes that channel's recall impossible."""
    trimmed, _ = allocate_by_budget(
        {"dense": [_chunk("a")]}, {"dense": 0.001}, total_token_budget=10, tokens_per_chunk=200
    )

    assert len(trimmed["dense"]) == 1


def test_profile_rejects_budgets_that_exceed_one() -> None:
    with pytest.raises(ValidationError, match="sum to <= 1.0"):
        _profile(channel_budgets={"dense": 0.9, "lexical": 0.5}).validate()


def test_profile_rejects_a_recall_channel_with_no_budget() -> None:
    with pytest.raises(ValidationError, match="no budget allocated"):
        _profile(recall_channels=["dense", "imaginary"], channel_budgets=dict(BUDGETS)).validate()


def test_profile_rejects_rerank_depth_above_recall_depth() -> None:
    with pytest.raises(ValidationError, match="top_k_rerank cannot exceed"):
        _profile(top_k_recall=10, top_k_rerank=20).validate()


def test_overlapping_crag_thresholds_are_rejected() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        CragThresholds(relevant_at=0.3, irrelevant_below=0.7).validate()


# -- fusion -----------------------------------------------------------------


def test_rrf_is_the_default_and_is_scale_independent() -> None:
    """Channel scores are not comparable — ts_rank_cd and cosine are different scales.

    RRF ranks by position, so a chunk both channels agree on outranks the chunk
    with the far higher raw dense score — which is the property that makes it
    usable as a default when the scales are not on the same footing.
    """
    agreed_id = uuid4()
    agreed = _chunk("agreed", score=0.4)
    agreed_dense = _synthetic_candidate(
        chunk_id=agreed_id,
        doc_id=agreed.doc_id,
        kb_id=agreed.kb_id,
        text="agreed",
        score=0.4,
        channel="dense",
        page_no=18,
    )
    agreed_lexical = _synthetic_candidate(
        chunk_id=agreed_id,
        doc_id=agreed.doc_id,
        kb_id=agreed.kb_id,
        text="agreed",
        score=0.01,
        channel="lexical",
        page_no=18,
    )
    dense_only_high = _chunk("dense only, high raw score", score=0.99)

    fusioner = Fusioner()
    fused = fusioner.fuse(
        {"lexical": [agreed_lexical], "dense": [dense_only_high, agreed_dense]},
        FusionConfig(),
    )

    assert [c.text for c in fused] == ["agreed", "dense only, high raw score"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "conflicting passage text"),
        ("page_no", 19),
        ("start_char_idx", 1),
        ("element_type", "table"),
    ],
)
def test_fusion_rejects_same_chunk_id_with_conflicting_evidence_metadata(
    field: str, value: object
) -> None:
    """A duplicate ID across recall channels must represent the same immutable evidence."""
    original = _chunk("stable passage")
    conflicting = replace(original, **{field: value})

    with pytest.raises(ValidationError, match="same chunk id has conflicting evidence lineage"):
        Fusioner().fuse(
            {"dense": [original], "lexical": [conflicting]},
            FusionConfig(),
        )


def test_weighted_fusion_instead_respects_the_raw_score() -> None:
    """The contrast that justifies making join_mode profile-selected."""
    agreed = _chunk("agreed", score=0.4)

    fused = Fusioner().fuse(
        {
            "lexical": [_chunk("agreed", channel="lexical", score=0.01)],
            "dense": [_chunk("other", score=0.99), agreed],
        },
        FusionConfig(join_mode="weighted"),
    )

    assert fused[0].text == "other"


def test_join_mode_is_profile_selected_not_hardcoded() -> None:
    fusioner = Fusioner()
    channels = {"dense": [_chunk("a", score=0.9), _chunk("b", score=0.1)]}

    concat = fusioner.fuse(channels, FusionConfig(join_mode="concatenate"))
    weighted = fusioner.fuse(channels, FusionConfig(join_mode="weighted"))

    assert concat[0].text == "a"
    assert weighted[0].text == "a"


def test_fusing_no_channels_returns_nothing() -> None:
    assert Fusioner().fuse({"dense": [], "lexical": []}, FusionConfig()) == []


# -- expansion --------------------------------------------------------------


def test_majority_sibling_merge_emits_a_parent_once() -> None:
    """Unconditional per-child expansion returns the same parent repeatedly."""
    parent_id = uuid4()
    siblings = [uuid4() for _ in range(3)]
    selected = [
        _synthetic_candidate(
            chunk_id=cid,
            doc_id=uuid4(),
            kb_id=uuid4(),
            text="child",
            score=0.5,
            channel="dense",
            parent_chunk_id=parent_id,
        )
        for cid in siblings[:2]
    ]
    parent = _synthetic_candidate(
        chunk_id=parent_id,
        doc_id=selected[0].doc_id,
        kb_id=selected[0].kb_id,
        text="the whole page",
        score=0.9,
        channel="dense",
        page_no=18,
    )

    expanded = expand_to_parents(
        selected, {parent_id: parent}, children_by_parent={parent_id: siblings}
    )

    assert len(expanded) == 1
    assert expanded[0].chunk_id == parent_id


def test_a_minority_of_children_does_not_emit_the_parent() -> None:
    parent_id = uuid4()
    siblings = [uuid4() for _ in range(4)]
    one_child = _synthetic_candidate(
        chunk_id=siblings[0],
        doc_id=uuid4(),
        kb_id=uuid4(),
        text="child",
        score=0.5,
        channel="dense",
        parent_chunk_id=parent_id,
    )

    assert majority_sibling_parents([one_child], {parent_id: siblings}) == set()


def test_dedupe_collapses_the_same_page_from_two_channels() -> None:
    doc = uuid4()
    a = _chunk("page text", doc=doc, page=18)
    b = _chunk("page text", doc=doc, page=18)

    assert len(dedupe_overlapping_parents([a, b])) == 1


# -- evidence selection -----------------------------------------------------


def test_evidence_selection_takes_the_configured_top_k() -> None:
    chunks = [_chunk(f"c{i}", score=1.0 - i / 100) for i in range(20)]

    assert len(select_evidence(chunks, _profile(top_k_generate=6))) == 6


def test_top_k_generate_is_not_hardcoded() -> None:
    """More context is not free: it dilutes attention and adds cost."""
    chunks = [_chunk(f"c{i}") for i in range(20)]

    assert len(select_evidence(chunks, _profile(top_k_generate=3))) == 3


# -- pipeline ---------------------------------------------------------------


def _judge(grades: list[int]):
    import re

    async def judge(query: str, messages: list[dict], _ctx: TenantContext) -> str:
        ids = re.findall(r'"chunk_id": "([^"]+)"', messages[1]["content"])
        return json.dumps(
            {"grades": [{"chunk_id": cid, "grade": g} for cid, g in zip(ids, grades, strict=False)]}
        )

    return judge


async def _gen(_query: str, _evidence: list[ScoredChunk], _ctx: TenantContext) -> str:
    return "Revenue Q2 was $4M [1]."


def _run(
    *,
    grades: list[int] | None = None,
    entailment=None,
    answer: str = "Revenue Q2 was $4M [1].",
    profile_overrides: dict | None = None,
    **pipeline_kwargs,
) -> RAGResult:
    ctx = _ctx()

    async def recall(_q, _p, _c):
        return [_chunk("Revenue Q2 was $4M.", score=0.9), _chunk("Revenue Q1 was $3M.", score=0.8)]

    async def generator(_q, _e, _c):
        return answer

    grader = (
        RelevanceGrader(judge=_judge(grades or [3, 1]), thresholds=CragThresholds())
        if grades is not None
        else None
    )

    profile = _profile(**(profile_overrides or {}))
    profile.tenant_id = ctx.tenant_id

    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=generator,
        grader=grader,
        entailment=entailment,
        **pipeline_kwargs,
    )
    return asyncio.run(pipeline.run("2025 Q2 revenue?", ctx, profile))


def test_pipeline_returns_a_cited_answer() -> None:
    result = _run()

    assert result.abstained is False
    assert len(result.citations) == 1
    assert result.citations[0].page_no == 18


def test_explain_records_each_stage() -> None:
    result = _run()

    explain = result.retrieval_explain
    assert explain.per_channel_counts == {"dense": 2}
    assert explain.channel_budgets == dict(BUDGETS)
    assert explain.rerank_depth == 2
    assert explain.citation_generated == 1


def test_the_refuse_path_still_returns_the_full_result_shape() -> None:
    """A branch that builds its own context is how refusal starts returning half-answers."""
    result = _run(grades=[0, 0])

    assert result.abstained is True
    assert result.gate is not None
    assert result.gate.action == GateAction.REFUSE
    assert result.citations == []
    assert result.retrieval_explain.gate_verdict == "irrelevant"


def test_the_ambiguous_path_proceeds_after_grading() -> None:
    result = _run(grades=[1, 1])

    assert result.gate.action == GateAction.RE_RETRIEVE
    assert result.abstained is False


def test_re_retrieval_reranks_new_and_original_evidence_together() -> None:
    """A new answering page must not sit after the original top-six cutoff."""
    ctx = _ctx()
    older = [_chunk(f"Earlier related page {i}", score=0.95 - i * 0.01) for i in range(8)]
    answering = _chunk("The updated clause provides the answer.", score=0.01)
    model_inputs: list[tuple[str, set[UUID]]] = []

    async def recall(query, _profile, _context):
        return [answering, older[0]] if query == "refined question" else older

    async def refine(_request, _context):
        return "refined question"

    async def rerank_model(query, chunks, _context):
        model_inputs.append((query, {item.chunk_id for item in chunks}))
        for item in chunks:
            item.score = 10.0 if item.chunk_id == answering.chunk_id else 0.1
        return sorted(chunks, key=lambda item: item.score, reverse=True)

    async def generate(_query, evidence, _context):
        return f"Answer from page {evidence[0].page_no} [1]."

    profile = _profile(
        channel_budgets={"dense": 1.0},
        top_k_generate=3,
        top_k_rerank=10,
        rerank_model_profile="synthetic-reranker",
    )
    profile.tenant_id = ctx.tenant_id
    result = asyncio.run(
        RAGPipeline(
            recall_channels={"dense": recall},
            generator=generate,
            grader=RelevanceGrader(judge=_judge([1] * 10), thresholds=CragThresholds()),
            reranker=rerank_model,
            refiner=refine,
        ).run("original question", ctx, profile)
    )

    ids = [item.chunk_id for item in result.evidence]
    assert ids[0] == answering.chunk_id
    assert older[0].chunk_id in ids
    assert result.retrieval_explain.re_retrieval_added_chunks == 1
    assert result.retrieval_explain.rerank_model_profile == "synthetic-reranker"
    assert result.retrieval_explain.rerank_depth == 9
    assert result.retrieval_explain.fusion_scores[str(answering.chunk_id)] == 10.0
    assert len(model_inputs) == 2
    assert model_inputs[-1][0] == "refined question"
    assert {answering.chunk_id, older[0].chunk_id} <= model_inputs[-1][1]


def test_re_retrieval_reranks_existing_candidates_for_refined_query() -> None:
    """A refined query can improve ranking even when recall returns no new IDs."""
    ctx = _ctx()
    first = _chunk("First-pass candidate")
    second = _chunk("Refined-query candidate")
    rerank_queries: list[str] = []

    async def recall(query, _profile, _context):
        return [second, first] if query == "refined question" else [first, second]

    async def refine(_request, _context):
        return "refined question"

    async def rerank_model(query, chunks, _context):
        rerank_queries.append(query)
        preferred = second.chunk_id if query == "refined question" else first.chunk_id
        for item in chunks:
            item.score = 1.0 if item.chunk_id == preferred else 0.0
        return sorted(chunks, key=lambda item: item.score, reverse=True)

    profile = _profile(
        channel_budgets={"dense": 1.0},
        top_k_generate=2,
        top_k_rerank=10,
        rerank_model_profile="synthetic-reranker",
    )
    profile.tenant_id = ctx.tenant_id
    result = asyncio.run(
        RAGPipeline(
            recall_channels={"dense": recall},
            generator=_gen,
            grader=RelevanceGrader(judge=_judge([1, 1]), thresholds=CragThresholds()),
            reranker=rerank_model,
            refiner=refine,
        ).run("original question", ctx, profile)
    )

    assert result.retrieval_explain.re_retrieval_added_chunks == 0
    assert result.evidence[0].chunk_id == second.chunk_id
    assert len(rerank_queries) == 2
    assert rerank_queries[-1] == "refined question"


def test_re_retrieval_preserves_first_pass_reranker_winner() -> None:
    """A bounded retry pool must retain the first pass's strongest reranked candidate."""
    ctx = _ctx()
    first_fused = _chunk("First pass fusion winner", score=0.99)
    first_reranked = _chunk("First pass reranker winner", score=0.98)
    refined_best = _chunk("Refined-query candidate", score=0.99)
    refined_other = _chunk("Another refined-query candidate", score=0.98)
    model_inputs: list[tuple[str, set[UUID]]] = []

    async def recall(query, _profile, _context):
        return (
            [refined_best, refined_other]
            if query == "refined question"
            else [first_fused, first_reranked]
        )

    async def refine(_request, _context):
        return "refined question"

    async def rerank_model(query, chunks, _context):
        model_inputs.append((query, {item.chunk_id for item in chunks}))
        preferred = (
            first_reranked.chunk_id
            if query == "original question"
            else refined_best.chunk_id
        )
        for item in chunks:
            item.score = 10.0 if item.chunk_id == preferred else 0.0
        return sorted(chunks, key=lambda item: item.score, reverse=True)

    profile = _profile(
        channel_budgets={"dense": 1.0},
        top_k_recall=10,
        top_k_rerank=2,
        top_k_generate=2,
        rerank_model_profile="synthetic-reranker",
    )
    profile.tenant_id = ctx.tenant_id
    result = asyncio.run(
        RAGPipeline(
            recall_channels={"dense": recall},
            generator=_gen,
            grader=RelevanceGrader(judge=_judge([1, 1]), thresholds=CragThresholds()),
            reranker=rerank_model,
            refiner=refine,
        ).run("original question", ctx, profile)
    )

    assert len(model_inputs) == 2
    assert first_reranked.chunk_id in model_inputs[0][1]
    assert refined_best.chunk_id in model_inputs[1][1]
    assert first_reranked.chunk_id in model_inputs[1][1]
    assert first_reranked.chunk_id in {item.chunk_id for item in result.evidence}


def test_re_retrieval_without_reranker_keeps_new_and_old_evidence() -> None:
    """The identity reranker must also give fresh evidence a Top-K opportunity."""
    ctx = _ctx()
    older = [_chunk(f"First pass page {i}") for i in range(4)]
    answering = _chunk("New second pass evidence")

    async def recall(query, _profile, _context):
        return [answering, older[0]] if query == "refined question" else older

    async def refine(_request, _context):
        return "refined question"

    async def generate(_query, _evidence, _context):
        return "The answer [1]."

    profile = _profile(channel_budgets={"dense": 1.0}, top_k_generate=2)
    profile.tenant_id = ctx.tenant_id
    result = asyncio.run(
        RAGPipeline(
            recall_channels={"dense": recall},
            generator=generate,
            grader=RelevanceGrader(judge=_judge([1] * 4), thresholds=CragThresholds()),
            refiner=refine,
        ).run("original question", ctx, profile)
    )

    assert [item.chunk_id for item in result.evidence] == [
        answering.chunk_id,
        older[0].chunk_id,
    ]
    assert result.retrieval_explain.re_retrieval_added_chunks == 1


def test_re_retrieval_rejects_conflicting_source_lineage() -> None:
    """A second read must not silently replace an already-seen source span."""
    ctx = _ctx()
    existing = _chunk("Original immutable page")
    conflicting = replace(existing, source_text="Modified text of different length")

    async def recall(query, _profile, _context):
        return [conflicting] if query == "refined question" else [existing]

    async def refine(_request, _context):
        return "refined question"

    profile = _profile(channel_budgets={"dense": 1.0})
    profile.tenant_id = ctx.tenant_id
    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=_gen,
        grader=RelevanceGrader(judge=_judge([1]), thresholds=CragThresholds()),
        refiner=refine,
    )
    with pytest.raises(ValidationError, match="cross-pass evidence has conflicting source lineage"):
        asyncio.run(pipeline.run("original question", ctx, profile))


def test_re_retrieval_rejects_duplicate_only_source_lineage_drift() -> None:
    """Conflicting data for an existing ID must fail even when retry adds no IDs."""
    ctx = _ctx()
    existing = _chunk("Original immutable page")
    conflicting = replace(existing, source_text="Modified immutable page")

    async def recall(query, _profile, _context):
        return [conflicting] if query == "refined question" else [existing]

    async def refine(_request, _context):
        return "refined question"

    profile = _profile(channel_budgets={"dense": 1.0})
    profile.tenant_id = ctx.tenant_id
    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=_gen,
        grader=RelevanceGrader(judge=_judge([1]), thresholds=CragThresholds()),
        refiner=refine,
    )
    with pytest.raises(ValidationError, match="cross-pass evidence has conflicting source lineage"):
        asyncio.run(pipeline.run("original question", ctx, profile))


def test_re_retrieval_rejects_duplicate_only_chunk_text_drift() -> None:
    """A repeated chunk ID cannot silently change chunk text within one source version."""
    ctx = _ctx()
    existing = _chunk("Original immutable page")
    conflicting = replace(existing, text="Modified chunk content", source_text=existing.source_text)

    async def recall(query, _profile, _context):
        return [conflicting] if query == "refined question" else [existing]

    async def refine(_request, _context):
        return "refined question"

    profile = _profile(channel_budgets={"dense": 1.0})
    profile.tenant_id = ctx.tenant_id
    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=_gen,
        grader=RelevanceGrader(judge=_judge([1]), thresholds=CragThresholds()),
        refiner=refine,
    )
    with pytest.raises(ValidationError, match="cross-pass evidence has conflicting source lineage"):
        asyncio.run(pipeline.run("original question", ctx, profile))


def test_an_unparseable_grader_response_defaults_to_irrelevant() -> None:
    """A missing grade must not become evidence that a passage was relevant."""

    async def garbage(_query, _messages, _ctx):
        return "I think it's fine."

    ctx = _ctx()

    async def recall(_q, _p, _c):
        return [_chunk("a")]

    grader = RelevanceGrader(judge=garbage, thresholds=CragThresholds())
    result = asyncio.run(
        grader.grade("q", [_chunk("a")], ctx),
    )
    del recall

    from ragos.retrieval.grader import GateVerdict

    assert result.verdict == GateVerdict.IRRELEVANT


def test_pipeline_rejects_a_profile_with_invalid_budgets() -> None:
    ctx = _ctx()

    async def recall(_q, _p, _c):
        return [_chunk("a")]

    async def generator(_q, _e, _c):
        return "a [1]."

    profile = _profile(channel_budgets={"dense": 0.99, "lexical": 0.99})
    pipeline = RAGPipeline(recall_channels={"dense": recall}, generator=generator)

    with pytest.raises(ValidationError):
        asyncio.run(pipeline.run("q", ctx, profile))


def test_evidence_set_exposes_the_delivered_chunks() -> None:
    chunk = _chunk("a")
    evidence = EvidenceSet(chunks=(chunk,))

    assert evidence.chunk_ids() == {chunk.chunk_id}
    assert evidence.by_id(chunk.chunk_id) is chunk


# -- eval split discipline --------------------------------------------------


def _case(split: str) -> EvalCase:
    source = _chunk("synthetic labelled evidence")
    return EvalCase(
        id=uuid4(),
        tenant_id=SYNTHETIC_TENANT_ID,
        question="q",
        gold_evidence=[
            GoldEvidence(
                doc_id=source.doc_id,
                grade=3,
                page_no=source.page_no,
                chunk_id=source.chunk_id,
                span=(source.start_char_idx, source.end_char_idx),
                source_identity=source.source_identity,
            )
        ],
        supported_claims=["a claim"],
        split=split,
    )


def test_split_cases_separates_dev_from_holdout() -> None:
    cases = [_case("dev"), _case("holdout"), _case("dev")]

    assert len(split_cases(cases, "dev")) == 2
    assert len(split_cases(cases, "holdout")) == 1


def _outcome(case: EvalCase, bundle) -> CaseOutcome:
    from ragos.retrieval.metrics import Grade

    return CaseOutcome(
        case_id=case.id,
        split=case.split,
        retrieved=[Grade(str(case.gold_evidence[0].doc_id), 3, case.gold_evidence[0].page_no)],
        evidence_hit=True,
        answer="a claim [1].",
        correct=True,
        generated_citations=1,
        supported_citations=1,
        cited_claims=1,
        supported_claims=1,
        rails_prompt_id=bundle.rails_prompt_version,
    )


def test_eval_run_reports_attribution_metrics_with_denominators() -> None:
    bundle = default_bundle()
    case = _case("holdout")

    result = run_eval(
        tenant_scope_key="t::u",
        bundle=bundle,
        cases=[case],
        outcomes=[_outcome(case, bundle)],
        split="holdout",
    )

    assert result.metrics["citation_accuracy"] == 1.0
    assert result.metrics["citation_completeness"] == 1.0
    assert result.metrics["evidence_hit_rate"] == 1.0
    assert result.bundle["rails_prompt_version"] == bundle.rails_prompt_version


def test_eval_refuses_to_run_unscoped() -> None:
    from ragos.core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError, match="scope_key"):
        run_eval(
            tenant_scope_key="",
            bundle=default_bundle(),
            cases=[],
            outcomes=[],
        )


def test_eval_rejects_grades_from_a_different_rails_prompt() -> None:
    """A prompt mismatch makes every retrieval metric incomparable."""
    bundle = default_bundle()
    case = _case("holdout")
    outcome = _outcome(case, bundle)
    outcome.rails_prompt_id = "rails-v1-someone-elses-edit"

    with pytest.raises(ValidationError, match="different rails prompt"):
        run_eval(
            tenant_scope_key="t::u",
            bundle=bundle,
            cases=[case],
            outcomes=[outcome],
        )


def test_the_bundle_freezes_every_version_including_the_rails_prompt() -> None:
    bundle = default_bundle(retriever_profile_version="r7")

    assert bundle.as_dict()["retriever_profile_version"] == "r7"
    assert bundle.bundle_id == default_bundle(retriever_profile_version="r7").bundle_id
    assert bundle.bundle_id != default_bundle(retriever_profile_version="r8").bundle_id


def test_a_candidate_scored_on_the_split_it_was_tuned_on_is_not_promotable() -> None:
    baseline = run_eval(
        tenant_scope_key="t::u",
        bundle=default_bundle(),
        cases=[],
        outcomes=[],
        split="holdout",
    )
    candidate = run_eval(
        tenant_scope_key="t::u",
        bundle=default_bundle(),
        cases=[],
        outcomes=[],
        split="dev",
    )

    verdict = gate(baseline=baseline, candidate=candidate)

    assert not verdict.promotable
    assert "tuned on" in verdict.regressions[0]


def test_the_rails_grader_is_validated_against_human_labels() -> None:
    result = validate_grader_agreement(
        rails_prompt_version="rails-v1",
        human_grades=[3, 2, 1, 0],
        grader_grades=[3, 2, 1, 0],
    )

    assert result["agreement"] == 1.0
    assert result["acceptable"]


def test_a_grader_that_disagrees_with_humans_is_flagged() -> None:
    result = validate_grader_agreement(
        rails_prompt_version="rails-v1",
        human_grades=[3, 2, 1, 0],
        grader_grades=[3, 3, 3, 3],
        min_agreement=0.8,
    )

    assert not result["acceptable"]
    assert result["agreement"] == 0.25


def test_an_unvalidated_grader_reports_as_unacceptable() -> None:
    result = validate_grader_agreement(
        rails_prompt_version="rails-v1",
        human_grades=[],
        grader_grades=[],
    )

    assert not result["acceptable"]
    assert result["sample_size"] == 0


# -- cross-tenant probes ----------------------------------------------------


def test_pipeline_refuses_an_empty_scope_key() -> None:
    ctx = _ctx()
    ctx = ctx.model_copy(update={"tenant_id": None})

    async def recall(_q, _p, _c):
        return []

    async def generator(_q, _e, _c):
        return ""

    pipeline = RAGPipeline(recall_channels={"dense": recall}, generator=generator)

    from ragos.core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError, match="scope_key"):
        asyncio.run(pipeline.run("q", ctx, _profile()))


def test_citation_status_is_explicit_on_the_object() -> None:
    """Verification state travels with the citation, not in a side log."""
    result = _run()

    assert result.citations[0].status in set(CitationStatus)
    assert result.citations[0].to_dict()["status"] == result.citations[0].status.value


def test_semantic_verification_is_opt_in_per_profile() -> None:
    async def entail(_claim: str, _text: str) -> SemanticVerdict:
        return SemanticVerdict.ENTAILED

    off = _run(entailment=entail, profile_overrides={"citation_verify_semantic": False})
    on = _run(entailment=entail, profile_overrides={"citation_verify_semantic": True})

    assert off.citations[0].status == CitationStatus.UNVERIFIED
    assert on.citations[0].status == CitationStatus.VERIFIED
