"""Rolling summarization and the memory rerank ladder.

Two claims, each of which is a rule rather than a tuning choice:

- ``window_n`` **has no default**. It is recorded on ``memory_profile_version`
  because recursive summarization is the weakest strategy in published memory
  benchmarks; constructing a summarizer without one is refused.
- memory recall ranks **cheapest signal first**, and the cross-encoder stage is
  bounded by ``top_n``. A ladder that ends in "score everything with a model" is the
  cost defect the ladder exists to prevent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.memory.rerank import mmr, rank_by_graph_distance, rank_by_mention, rank_facts
from ragos.memory.summarizer import RollingSummarizer, Turn, build_summarizer

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid4(), tenant_slug="acme", actor_id="u_1", trace_id="t")


def _turns(count: int) -> list[Turn]:
    return [
        Turn(index=i, role="user" if i % 2 == 0 else "assistant", text=f"turn {i}")
        for i in range(count)
    ]


def _summarize_to(text: str, recorder: list):
    async def summarize(body: str, tenant_ctx: TenantContext) -> str:
        recorder.append((body, tenant_ctx))
        return text

    return summarize


# -- window_n has no default ------------------------------------------------


def test_a_summarizer_without_a_window_is_refused() -> None:
    with pytest.raises(ValidationError, match="window_n"):
        RollingSummarizer(window_n=None, version="mpv-1")


def test_a_summarizer_without_a_profile_version_is_refused() -> None:
    """A summary must be traceable to the evaluated policy that produced it."""
    with pytest.raises(ValidationError, match="version"):
        RollingSummarizer(window_n=6, version=None)


def test_a_zero_window_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        RollingSummarizer(window_n=0, version="mpv-1")


# -- windowing --------------------------------------------------------------


def test_recent_turns_stay_verbatim() -> None:
    summarizer = RollingSummarizer(window_n=2, version="mpv-1")

    verbatim = summarizer.verbatim_turns(_turns(6))

    assert [t.index for t in verbatim] == [4, 5]
    assert [t.index for t in summarizer.older_turns(_turns(6))] == [0, 1, 2, 3]


def test_a_short_conversation_is_not_summarized_at_all() -> None:
    summarizer = RollingSummarizer(window_n=6, version="mpv-1")

    assert summarizer.older_turns(_turns(4)) == []


def test_the_verbatim_window_is_the_tail_not_the_head() -> None:
    """The most recent turns are what the summary has to protect."""
    summarizer = RollingSummarizer(window_n=3, version="mpv-1")

    assert summarizer.verbatim_turns(_turns(10))[-1].index == 9


# -- summarization behaviour ------------------------------------------------


def test_only_turns_older_than_the_window_are_summarized() -> None:
    recorder: list = []
    summarizer = RollingSummarizer(
        window_n=2, version="mpv-1", summarize=_summarize_to("compressed", recorder)
    )

    result = asyncio.run(summarizer.summarize_older(_turns(6), tenant_ctx=_ctx()))

    assert result.applied is True
    assert result.turn_range_start == 0
    assert result.turn_range_end == 3
    assert "turn 0" in recorder[0][0]
    # The verbatim window is not summarized: those turns stay as they are.
    assert "turn 4" not in recorder[0][0]
    assert "turn 5" not in recorder[0][0]


def test_a_boundary_already_covered_is_a_no_op() -> None:
    """The writeback is a retried task; a retry must not re-summarize the same turns."""
    recorder: list = []
    summarizer = RollingSummarizer(
        window_n=2, version="mpv-1", summarize=_summarize_to("compressed", recorder)
    )

    result = asyncio.run(
        summarizer.summarize_older(
            _turns(6),
            tenant_ctx=_ctx(),
            already_covered_through=3,
            previous_summary="earlier",
        )
    )

    assert result.is_noop is True
    assert result.summary == "earlier"
    assert recorder == []


def test_an_empty_summary_keeps_the_turns_verbatim() -> None:
    """An empty summary silently drops the turns it stands in for."""
    summarizer = RollingSummarizer(
        window_n=2, version="mpv-1", summarize=_summarize_to("   ", [])
    )

    result = asyncio.run(summarizer.summarize_older(_turns(6), tenant_ctx=_ctx()))

    assert result.is_noop is True
    assert result.notes


def test_a_summary_is_appended_to_the_previous_one() -> None:
    summarizer = RollingSummarizer(
        window_n=2, version="mpv-1", summarize=_summarize_to("newer", [])
    )

    result = asyncio.run(
        summarizer.summarize_older(
            _turns(6), tenant_ctx=_ctx(), already_covered_through=0, previous_summary="older"
        )
    )

    assert result.summary == "older\nnewer"


def test_summarizing_without_a_callable_is_refused() -> None:
    summarizer = RollingSummarizer(window_n=2, version="mpv-1")

    with pytest.raises(ValidationError, match="callable"):
        asyncio.run(summarizer.summarize_older(_turns(6), tenant_ctx=_ctx()))


def test_the_gateway_call_is_metered_to_the_tenant() -> None:
    seen: list = []

    async def complete(messages: list[dict], tenant_ctx: TenantContext) -> str:
        seen.append((messages, tenant_ctx))
        return "compressed"

    summarizer = build_summarizer(complete, window_n=2, version="mpv-1")
    ctx = _ctx()

    asyncio.run(summarizer.summarize_older(_turns(4), tenant_ctx=ctx))

    assert seen[0][1] is ctx


# -- the ladder -------------------------------------------------------------


def _fact(text: str, *, mentions: int = 1, decay: float = 1.0, confidence: float = 0.9):
    from ragos.memory.types import FactCategory, MemoryFact

    return MemoryFact(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        fact=text,
        category=FactCategory.PREFERENCE.value,
        confidence=confidence,
        decay_score=decay,
        mention_count=mentions,
        valid_from=NOW,
        ingested_at=NOW,
    )


def test_mention_frequency_is_the_first_signal() -> None:
    low = _fact("mentioned once", mentions=1)
    high = _fact("mentioned often", mentions=9)

    assert [f.fact for f in rank_by_mention([low, high])][0] == "mentioned often"


def test_an_unknown_graph_distance_sorts_last() -> None:
    """A missing edge is not evidence of closeness."""
    linked = _fact("linked")
    unlinked = _fact("unlinked")

    ranked = rank_by_graph_distance([unlinked, linked], {linked.id: 1})

    assert [f.fact for f in ranked][0] == "linked"


def test_mmr_removes_a_paraphrase_before_the_expensive_stage() -> None:
    a = _fact("the user prefers metric units")
    b = _fact("the user prefers metric units")
    c = _fact("the user's billing contact is finance@example.com")

    selected = mmr([a, b, c], lambda_=0.5)

    # The paraphrase is demoted below the unrelated fact rather than dropped:
    # diversification is about redundancy, not about discarding evidence.
    assert [f.fact for f in selected] == [
        "the user prefers metric units",
        "the user's billing contact is finance@example.com",
        "the user prefers metric units",
    ]


def test_the_cross_encoder_only_sees_the_bounded_survivors() -> None:
    """An unbounded final stage turns the ladder back into scoring everything."""
    seen: list = []

    async def cross_encoder(query: str, facts):
        seen.append([f.fact for f in facts])
        return [1.0] * len(facts)

    facts = [_fact(f"fact number {i}") for i in range(20)]

    ranked = asyncio.run(rank_facts("query", facts, cross_encoder=cross_encoder, top_n=5))

    assert len(ranked) == 5
    assert len(seen[0]) == 5


def test_the_cross_encoder_orders_the_final_set() -> None:
    weak = _fact("weak signal", decay=0.9)
    strong = _fact("strong signal", decay=0.9)

    async def cross_encoder(query, facts):
        return [0.1 if f is weak else 0.9 for f in facts]

    ranked = asyncio.run(rank_facts("query", [weak, strong], cross_encoder=cross_encoder, top_n=5))

    assert ranked[0].fact.fact == "strong signal"
    assert ranked[0].stage == "cross_encoder"


def test_without_a_cross_encoder_the_ladder_stops_at_mmr() -> None:
    ranked = asyncio.run(rank_facts("query", [_fact("a"), _fact("b")], top_n=5))

    assert ranked[0].stage == "mmr"


def test_graph_distance_participates_when_supplied() -> None:
    near = _fact("near", mentions=1)
    far = _fact("far", mentions=9)

    ranked = asyncio.run(
        rank_facts("query", [near, far], graph_distance={near.id: 1, far.id: 5}, top_n=5)
    )

    assert {r.fact.fact for r in ranked} == {"near", "far"}


def test_ranking_nothing_returns_nothing() -> None:
    assert asyncio.run(rank_facts("query", [])) == []


def test_a_never_expiring_fact_reports_no_expiry() -> None:
    from ragos.memory.decay import days_until_expiry

    assert days_until_expiry(valid_until=None, now=NOW) is None
    assert days_until_expiry(valid_until=NOW + timedelta(days=2), now=NOW) == pytest.approx(2)
