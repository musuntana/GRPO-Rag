"""Memory context assembly, durable writeback enqueue, and the memory channel.

Three properties under test, one per boundary:

- **context**: memory is rendered as its own labelled section, never interleaved
  with knowledge evidence, and a read with no actor refuses
- **writeback**: it becomes a task row with a conversation-derived idempotency
  key, not an inline call whose failure leaves memory silently empty
- **channel**: memory recall is a *parallel* channel scoped to one actor, and an
  empty memory channel takes nothing from the other channels' budgets
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.memory.manager import MEMORY_WRITEBACK, MemoryManager
from ragos.memory.summarizer import Turn
from ragos.memory.types import FactCategory, MemoryFact
from ragos.retrieval.fusion import allocate_by_budget
from ragos.retrieval.recall.memory import MemoryRecall
from ragos.retrieval.types import RetrieverProfile, ScoredChunk

TENANT_ID = uuid4()
USER_ID = uuid4()
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _ctx(*, user_id=USER_ID) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-ctx",
        user_id=user_id,
    )


def _fact(text: str, **overrides) -> MemoryFact:
    fields = {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "fact": text,
        "category": FactCategory.PREFERENCE.value,
        "confidence": 0.9,
        "decay_score": 1.0,
        "valid_from": NOW,
        "ingested_at": NOW,
    }
    fields.update(overrides)
    return MemoryFact(**fields)


class _FakeFactStore:
    def __init__(self, facts=None, tenant_ctx=None) -> None:
        self.tenant_ctx = tenant_ctx or _ctx()
        self.facts = list(facts or [])
        self.recalled: list[dict] = []
        self.touched: list[dict] = []

    def recall(self, **kwargs):
        self.recalled.append(kwargs)
        return list(self.facts[: kwargs.get("limit", len(self.facts))])

    def touch(self, **kwargs) -> int:
        self.touched.append(kwargs)
        return len(kwargs.get("fact_ids", []))

    def record(self, **kwargs):
        return []


class _FakeTaskRepo:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return kwargs


def _manager(store=None) -> MemoryManager:
    return MemoryManager(fact_store=store or _FakeFactStore())


# -- context ----------------------------------------------------------------


def test_memory_renders_as_its_own_labelled_section() -> None:
    """A fact and a passage are indistinguishable once concatenated."""
    from ragos.memory.manager import MEMORY_SECTION_LABEL, MemoryContext

    context = MemoryContext(
        facts=[_fact("uses metric units")],
        conversation_summary="we agreed on a quarterly review",
        recent_messages=[Turn(index=0, role="user", text="what now?")],
    )

    rendered = context.render()

    assert MEMORY_SECTION_LABEL in rendered
    assert "uses metric units" in rendered
    assert "we agreed on a quarterly review" in rendered
    assert "what now?" in rendered


def test_an_empty_memory_context_renders_nothing_at_all() -> None:
    """A section that says "no memory" invites the model to comment on the absence."""
    from ragos.memory.manager import MemoryContext

    assert MemoryContext().render() == ""
    assert MemoryContext().is_empty() is True


def test_reading_memory_without_a_actor_refuses() -> None:
    manager = _manager(_FakeFactStore(tenant_ctx=_ctx(user_id=None)))

    with pytest.raises(PermissionDeniedError, match="user_id"):
        asyncio.run(manager.build_context(user_id=None))


def test_the_facts_used_in_a_prompt_have_their_recency_clock_reset() -> None:
    """Otherwise decay is a function of ingestion time alone."""
    store = _FakeFactStore([_fact("uses metric units")])
    manager = _manager(store)

    asyncio.run(manager.build_context(user_id=USER_ID, query="units", now=NOW))

    assert store.touched and store.touched[0]["fact_ids"]
    assert store.recalled[0]["scope_key"] == manager.tenant_ctx.require_private_scope_key()


def test_the_context_carries_facts_in_ranked_order() -> None:
    store = _FakeFactStore([_fact("a fact"), _fact("another fact")])
    manager = _manager(store)

    context = asyncio.run(manager.build_context(user_id=USER_ID, query="fact", now=NOW))

    assert [f.fact for f in context.facts] == ["a fact", "another fact"]
    assert context.user_id == USER_ID


# -- writeback enqueue ------------------------------------------------------


def test_writeback_is_enqueued_as_a_durable_task_with_a_derived_key() -> None:
    tasks = _FakeTaskRepo()
    manager = _manager()
    conversation_id = uuid4()

    manager.enqueue_writeback(
        task_repo=tasks,
        conversation_id=conversation_id,
        user_id=USER_ID,
        message_count=12,
        window_n=6,
        profile_version="mpv-3",
    )

    task = tasks.enqueued[0]
    assert task["kind"] == MEMORY_WRITEBACK
    # Derived from the conversation, so a retried enqueue lands on one row.
    assert task["idempotency_key"] == f"memory-writeback:{conversation_id}:12"
    assert task["payload"]["user_id"] == str(USER_ID)
    assert task["payload"]["window_n"] == 6
    assert task["payload"]["profile_version"] == "mpv-3"


def test_two_enqueue_of_the_same_conversation_produce_the_same_key() -> None:
    tasks = _FakeTaskRepo()
    manager = _manager()
    conversation_id = uuid4()

    for _ in range(2):
        manager.enqueue_writeback(
            task_repo=tasks,
            conversation_id=conversation_id,
            user_id=USER_ID,
            message_count=4,
            window_n=6,
            profile_version="mpv-1",
        )

    assert tasks.enqueued[0]["idempotency_key"] == tasks.enqueued[1]["idempotency_key"]


def test_enqueueing_without_a_recorded_window_is_refused() -> None:
    manager = _manager()

    with pytest.raises(ValidationError, match="window_n"):
        manager.enqueue_writeback(
            task_repo=_FakeTaskRepo(),
            conversation_id=uuid4(),
            user_id=USER_ID,
            message_count=1,
            window_n=0,
            profile_version="mpv-1",
        )


def test_enqueueing_without_an_actor_refuses() -> None:
    manager = _manager(_FakeFactStore(tenant_ctx=_ctx(user_id=None)))

    with pytest.raises(PermissionDeniedError, match="user_id"):
        manager.enqueue_writeback(
            task_repo=_FakeTaskRepo(),
            conversation_id=uuid4(),
            user_id=None,
            message_count=1,
            window_n=6,
            profile_version="mpv-1",
        )


# -- the memory channel -----------------------------------------------------


class _FakeSearchRepo:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def _profile(**overrides) -> RetrieverProfile:
    fields = {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "recall_channels": ["lexical", "memory"],
        "channel_budgets": {"lexical": 0.5, "memory": 0.5},
        "top_k_recall": 10,
    }
    fields.update(overrides)
    return RetrieverProfile(**fields)


def test_memory_recall_is_scoped_to_the_actor() -> None:
    repo = _FakeSearchRepo()
    channel = MemoryRecall(repo)
    ctx = _ctx()

    asyncio.run(channel.recall("what do I prefer?", ctx, _profile()))

    assert repo.calls[0]["tier"].value == "memory"
    assert repo.calls[0]["user_id"] == USER_ID
    assert repo.calls[0]["scope_key"] == ctx.require_private_scope_key()


def test_memory_recall_without_an_actor_refuses() -> None:
    """Returning the tenant's facts to one end user is a disclosure."""
    channel = MemoryRecall(_FakeSearchRepo())

    with pytest.raises(PermissionDeniedError, match="user_id"):
        asyncio.run(channel.recall("q", _ctx(user_id=None), _profile()))


def test_memory_hits_carry_their_own_layer() -> None:
    """Fusion's layer boosts must be able to prefer documentation over recollection."""
    from ragos.storage.repositories.search import SearchHit

    hit = SearchHit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        kb_id=None,
        layer="L1",
        body="uses metric units",
        metadata_json={},
        rank=0.4,
    )
    channel = MemoryRecall(_FakeSearchRepo([hit]))

    chunks = asyncio.run(channel.recall("units", _ctx(), _profile()))

    assert [c.channel for c in chunks] == ["memory"]
    assert [c.layer for c in chunks] == ["MEM"]


def test_an_empty_memory_channel_does_not_take_its_budget() -> None:
    """The rule that keeps dense from dominating by default."""
    dense = [
        ScoredChunk(
            chunk_id=uuid4(), doc_id=uuid4(), kb_id=None, text="d", score=1.0, channel="dense"
        )
    ]

    trimmed, allocations = allocate_by_budget(
        {"dense": dense, "memory": []}, {"dense": 0.5, "memory": 0.5}
    )
    by_channel = {a.channel: a for a in allocations}

    assert by_channel["memory"].returned == 0
    # The memory budget stays unspent instead of being handed to dense recall.
    assert by_channel["dense"].budget == 0.5
    assert by_channel["dense"].returned == 1
    assert len(trimmed["dense"]) == 1
    assert trimmed["memory"] == []
