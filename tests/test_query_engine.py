"""Query planning and the shared refine node.

Architecture §8.2 (strategies) and §8.4 (one refine node, three gate paths).

The claims under test: a follow-up question is made standalone before recall; a
HyDE hypothesis that merely restates the question is not spent as if it were an
improvement; decomposition degrades rather than returning nothing; and all three
gate outcomes produce the same result shape with a populated ``refined_query``.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import CitationStatus
from ragos.retrieval.grader import CragThresholds, GateAction, RelevanceGrader
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.pipeline import RAGPipeline
from ragos.retrieval.query import (
    ContextualRewriter,
    ConversationTurn,
    Decomposer,
    HyDE,
    QueryEngine,
    default_history,
    parse_decomposition,
)
from ragos.retrieval.refine import RefineRequest, SentenceRefiner, deterministic_refine
from ragos.retrieval.types import RetrieverProfile, ScoredChunk

TEST_TENANT_ID = uuid4()


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=TEST_TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-1",
    )


def _complete(reply: str, recorder: list | None = None):
    async def complete(messages: list[dict], tenant_ctx: TenantContext) -> str:
        if recorder is not None:
            recorder.append((messages, tenant_ctx))
        return reply

    return complete


# -- history handling -------------------------------------------------------


def test_history_is_truncated_from_the_front() -> None:
    """The follow-up points back at the most recent turn, not the first one."""
    turns = tuple(
        ConversationTurn(role="user" if i % 2 == 0 else "assistant", text=f"t{i}") for i in range(8)
    )

    kept = default_history(turns, 4)

    assert [t.text for t in kept] == ["t4", "t5", "t6", "t7"]


def test_a_first_turn_question_is_not_rewritten() -> None:
    """No history means nothing to resolve, so no model call is made."""
    calls: list = []
    engine = QueryEngine(rewriter=ContextualRewriter(_complete("rewritten", calls)))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("what is the retention policy?", ctx))

    assert plan.primary == "what is the retention policy?"
    assert plan.used_history is False
    assert calls == []


def test_a_follow_up_question_is_made_standalone() -> None:
    calls: list = []
    engine = QueryEngine(rewriter=ContextualRewriter(_complete("EU data residency rules", calls)))
    ctx = _ctx()

    plan = asyncio.run(
        engine.plan(
            "And for the EU?",
            ctx,
            history=(
                ConversationTurn(role="user", text="Where is customer data stored?"),
                ConversationTurn(role="assistant", text="In us-east-1."),
            ),
        )
    )

    assert plan.primary == "EU data residency rules"
    assert plan.queries == ("EU data residency rules",)
    assert plan.used_history is True
    assert calls[0][1] is ctx, "the rewrite call must be metered to the tenant"


def test_a_failing_rewrite_falls_back_instead_of_returning_an_unretrievable_question() -> None:
    async def broken(_messages, _ctx):
        raise RuntimeError("provider down")

    engine = QueryEngine(rewriter=ContextualRewriter(broken))
    ctx = _ctx()

    plan = asyncio.run(
        engine.plan(
            "And for the EU?",
            ctx,
            history=(ConversationTurn(role="user", text="Where is customer data stored?"),),
        )
    )

    assert "Where is customer data stored?" in plan.primary
    assert plan.primary.endswith("And for the EU?")


def test_an_empty_rewrite_falls_back_to_the_deterministic_path() -> None:
    engine = QueryEngine(rewriter=ContextualRewriter(_complete("   ")))
    ctx = _ctx()

    plan = asyncio.run(
        engine.plan(
            "And then?",
            ctx,
            history=(ConversationTurn(role="user", text="Tell me about key rotation"),),
        )
    )

    assert plan.primary == "Tell me about key rotation And then?"


# -- HyDE -------------------------------------------------------------------


def test_hyde_recalls_the_hypothesis_but_keeps_the_real_question_for_grading() -> None:
    engine = QueryEngine(hyde=HyDE(_complete("Retention is 30 days for all business tiers.")))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("how long do we keep logs?", ctx, strategy="hyde"))

    assert plan.strategy == "hyde"
    assert plan.queries == ("Retention is 30 days for all business tiers.",)
    # Grading a hypothesis against a hypothesis says nothing about the evidence.
    assert plan.primary == "how long do we keep logs?"


def test_a_hyde_passage_that_restates_the_question_is_reported_not_spent() -> None:
    engine = QueryEngine(hyde=HyDE(_complete("how long do we keep logs?")))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("how long do we keep logs?", ctx, strategy="hyde"))

    assert plan.queries == ("how long do we keep logs?",)
    assert plan.notes, "a wasted call must be visible in the explain"


def test_hyde_without_a_model_degrades_to_the_question() -> None:
    engine = QueryEngine(hyde=HyDE(None))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("q?", ctx, strategy="hyde"))

    assert plan.queries == ("q?",)
    assert plan.hypothetical is None


def test_an_empty_hyde_passage_is_never_used_as_a_query() -> None:
    """An empty embedding vector query returns confident nonsense."""
    engine = QueryEngine(hyde=HyDE(_complete("   ")))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("q?", ctx, strategy="hyde"))

    assert plan.queries == ("q?",)


# -- decomposition ----------------------------------------------------------


def test_decomposition_splits_on_a_second_intent() -> None:
    reply = "how do I rotate keys\nwhat does rotation cost"
    engine = QueryEngine(decomposer=Decomposer(_complete(reply)))
    ctx = _ctx()

    plan = asyncio.run(
        engine.plan("how do I rotate keys and what does rotation cost?", ctx, strategy="decompose")
    )

    assert plan.sub_queries == ("how do I rotate keys", "what does rotation cost")
    assert plan.queries == plan.sub_queries
    assert plan.primary == "how do I rotate keys and what does rotation cost?"


def test_decomposition_is_capped() -> None:
    reply = "\n".join(f"part {i}" for i in range(9))
    engine = QueryEngine(decomposer=Decomposer(_complete(reply), max_sub_queries=3))
    ctx = _ctx()

    plan = asyncio.run(
        engine.plan("a and b and c and d and e and f and g and h and i", ctx, strategy="decompose")
    )

    assert len(plan.queries) == 3


def test_a_single_intent_question_records_that_decomposition_did_nothing() -> None:
    engine = QueryEngine(decomposer=Decomposer(_complete("what is the retention policy")))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("what is the retention policy?", ctx, strategy="decompose"))

    assert plan.strategy == "direct"
    assert plan.queries == ("what is the retention policy?",)
    assert plan.notes


def test_decomposition_never_leaves_the_pipeline_without_a_query() -> None:
    engine = QueryEngine(decomposer=Decomposer(_complete("")))
    ctx = _ctx()

    plan = asyncio.run(engine.plan("what is the retention policy?", ctx, strategy="decompose"))

    assert plan.queries == ("what is the retention policy?",)


def test_a_json_wrapped_decomposition_is_accepted() -> None:
    """Asking for plain lines, models return JSON often enough that rejecting it wastes a call."""
    parsed = parse_decomposition(json.dumps({"sub_queries": ["a", "b"]}))

    assert parsed == ["a", "b"]


def test_an_empty_query_is_rejected_before_any_model_call() -> None:
    calls: list = []
    engine = QueryEngine(rewriter=ContextualRewriter(_complete("x", calls)))

    with pytest.raises(ValidationError):
        asyncio.run(engine.plan("   ", _ctx()))

    assert calls == []


# -- refine node ------------------------------------------------------------


def test_deterministic_refine_normalizes_without_inventing_meaning() -> None:
    assert deterministic_refine("  What is the   policy?  ") == "What is the policy"
    assert deterministic_refine("") == ""


def test_the_refine_node_keeps_a_long_question_readable() -> None:
    long_question = "why " + ("very " * 200) + "long"

    refined = deterministic_refine(long_question)

    assert len(refined) <= 1000
    assert not refined.endswith("ver")


def test_a_failing_refine_call_falls_back_to_normalization() -> None:
    async def broken(_messages, _ctx):
        raise RuntimeError("provider down")

    request = RefineRequest(query="What is the policy?", verdict=None, action=GateAction.PROCEED)

    assert asyncio.run(SentenceRefiner(broken).refine(request, _ctx())) == "What is the policy"


def test_a_refused_answer_still_carries_a_refined_query() -> None:
    """All three gate paths pass through the refine node; an absent value means a
    branch grew its own context."""
    ctx = _ctx()

    async def recall(_q, _p, _c):
        return [_chunk("irrelevant")]

    async def generator(_q, _e, _c):
        return "should not run [1]."

    async def judge(_query, _messages, _ctx):
        return json.dumps({"grades": [{"chunk_id": cid, "grade": 0}
                                     for cid in _chunk_ids(_messages)]})

    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=generator,
        grader=RelevanceGrader(judge=judge, thresholds=CragThresholds()),
        refiner=SentenceRefiner(_complete("the retention policy")).refine,
    )
    result = asyncio.run(pipeline.run("what is the retention policy?", ctx, _profile()))

    assert result.abstained is True
    assert result.retrieval_explain.refined_query == "the retention policy"


# -- pipeline integration ---------------------------------------------------


def _chunk(text: str, *, score: float = 0.9) -> ScoredChunk:
    chunk_id, doc_id, kb_id = uuid4(), uuid4(), uuid4()
    identity = EvidenceIdentity(
        tenant_id=TEST_TENANT_ID,
        kb_id=kb_id,
        document_id=doc_id,
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        chunk_id=chunk_id,
        representation_char_count=100000,
        parent_unit="page",
        page_count=40,
    )
    return ScoredChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        kb_id=kb_id,
        text=text,
        score=score,
        channel="dense",
        layer="L1",
        page_no=18,
        page_count=40,
        start_char_idx=10,
        end_char_idx=10 + len(text),
        source_identity=identity,
        source_text=text,
    )


def _profile(**overrides) -> RetrieverProfile:
    ctx_tenant = TEST_TENANT_ID
    base = {
        "id": uuid4(),
        "tenant_id": ctx_tenant,
        "recall_channels": ["dense"],
        "channel_budgets": {"dense": 1.0},
    }
    base.update(overrides)
    return RetrieverProfile(**base)


def test_decomposed_queries_reach_every_channel_and_deduplicate() -> None:
    """The same passage must not occupy two rerank slots because the question was multi-part."""
    seen: list[str] = []
    shared = _chunk("shared passage", score=0.5)

    async def recall(query, _profile, _ctx):
        seen.append(query)
        return [shared, _chunk(f"hit for {query}", score=0.4)]

    async def generator(_q, _e, _c):
        return "answer [1]."

    engine = QueryEngine(decomposer=Decomposer(_complete("alpha\nbeta")))
    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=generator,
        query_engine=engine,
    )
    profile = _profile(query_strategy="decompose")
    ctx = _ctx()
    profile.tenant_id = ctx.tenant_id

    result = asyncio.run(pipeline.run("alpha and beta", ctx, profile))

    assert seen == ["alpha", "beta"]
    assert result.retrieval_explain.sub_queries == ["alpha", "beta"]
    assert result.retrieval_explain.recall_queries == ["alpha", "beta"]
    # 2 shared/unique chunks plus the one shared across both queries, once.
    assert result.retrieval_explain.per_channel_counts["dense"] == 3


def test_the_ambiguous_path_re_retrieves_once_and_reports_what_it_added() -> None:
    ctx = _ctx()
    attempts: list[str] = []

    async def recall(query, _profile, _ctx):
        attempts.append(query)
        if len(attempts) == 1:
            return [_chunk("partial")]
        return [_chunk("the missing half", score=0.95)]

    async def generator(_q, _e, _c):
        return "answer [1]."

    async def judge(query, messages, _ctx):
        ids = _chunk_ids(messages)
        # Grade the first pass as unusable, and accept the re-retrieved evidence.
        grades = 1 if len(attempts) == 1 else 3
        return json.dumps({"grades": [{"chunk_id": cid, "grade": grades} for cid in ids]})

    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=generator,
        grader=RelevanceGrader(judge=judge, thresholds=CragThresholds()),
    )
    profile = _profile()
    profile.tenant_id = ctx.tenant_id

    result = asyncio.run(pipeline.run("what did we agree?", ctx, profile))

    explain = result.retrieval_explain
    assert explain.gate_action == GateAction.PROCEED
    assert explain.gate_verdict == "relevant"
    assert result.abstained is False
    assert explain.re_retrieval_attempted is True
    assert explain.re_retrieval_added_chunks == 1
    # Two sweeps, not a loop: a gate that has said "ambiguous" twice has said the
    # corpus does not contain the answer.
    assert len(attempts) == 2


def test_a_proceed_gate_does_not_re_retrieve() -> None:
    ctx = _ctx()
    attempts: list[str] = []

    async def recall(query, _profile, _ctx):
        attempts.append(query)
        return [_chunk("answer")]

    async def generator(_q, _e, _c):
        return "answer [1]."

    async def judge(query, messages, _ctx):
        ids = _chunk_ids(messages)
        return json.dumps({"grades": [{"chunk_id": cid, "grade": 3} for cid in ids]})

    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=generator,
        grader=RelevanceGrader(judge=judge, thresholds=CragThresholds()),
    )
    profile = _profile()
    profile.tenant_id = ctx.tenant_id

    result = asyncio.run(pipeline.run("what is the policy?", ctx, profile))

    assert result.retrieval_explain.gate_action == GateAction.PROCEED
    assert result.retrieval_explain.re_retrieval_attempted is False
    assert len(attempts) == 1
    assert result.citations[0].status in (CitationStatus.VERIFIED, CitationStatus.UNVERIFIED)


def _chunk_ids(messages: list[dict]) -> list[str]:
    import re

    return re.findall(r'"chunk_id": "([^"]+)"', messages[1]["content"])
