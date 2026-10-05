"""One chat turn: memory in, events out, writeback scheduled.

The claims under test:

- the turn **refuses** without a resolvable actor rather than answering with the
  tenant's memory
- events are emitted in the documented order, and the retrieval trace is present
  even when the gate abstains
- the answer and its citations are persisted before the run is reported finished
- the writeback is *scheduled*, and the event says so — there is no
  ``memory_updated``, because the writeback has not happened yet
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import Citation, CitationStatus
from ragos.retrieval.pipeline import RAGResult, RetrievalExplain
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.runtime.chat import ChatDependencies, ChatService, _segment
from ragos.runtime.events import EventLog, EventSink

TENANT_ID = uuid4()
USER_ID = uuid4()
CONVERSATION_ID = uuid4()
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _ctx(*, user_id=USER_ID) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-chat",
        user_id=user_id,
    )


def _result(*, answer: str = "Revenue was $4M [1].", abstained: bool = False) -> RAGResult:
    chunk = ScoredChunk(
        chunk_id=uuid4(),
        doc_id=uuid4(),
        kb_id=None,
        text="evidence",
        score=0.9,
        channel="lexical",
        page_no=3,
    )
    explain = RetrievalExplain(
        rewritten_query="revenue",
        refined_query="revenue",
        per_channel_counts={"lexical": 1, "memory": 2},
        channel_budgets={"lexical": 0.5, "memory": 0.5},
        rerank_depth=1,
        gate_verdict="irrelevant" if abstained else "relevant",
        gate_confidence=0.9,
        gate_action="refuse" if abstained else "proceed",
        citation_supported=1,
        citation_generated=1,
        parents_resolved=1,
    )
    citations = (
        []
        if abstained
        else [
            Citation(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                page_no=3,
                start_char_idx=1,
                end_char_idx=40,
                claim=answer,
                text="evidence",
                status=CitationStatus.VERIFIED,
                marker_index=1,
            )
        ]
    )
    return RAGResult(
        answer=answer,
        citations=citations,
        retrieval_explain=explain,
        evidence=(chunk,),
        gate=None,
        abstained=abstained,
        token_usage={},
    )


class _FakeRetrieval:
    def __init__(self, result: RAGResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def query(self, **kwargs):
        from ragos.runtime.retrieval import RetrievalOutcome

        self.calls.append(kwargs)
        profile = RetrieverProfile(
            id=uuid4(),
            tenant_id=TENANT_ID,
            recall_channels=["lexical"],
            channel_budgets={"lexical": 1.0},
        )
        return RetrievalOutcome(result=self.result, profile=profile, warnings=[])


class _FakeMemoryManager:
    def __init__(self, facts=None) -> None:
        from ragos.memory.manager import MemoryContext

        self.facts = list(facts or [])
        self.context = MemoryContext(
            facts=self.facts, user_id=USER_ID, conversation_summary=None, recent_messages=[]
        )
        self.enqueued: list[dict] = []

    async def build_context(self, **kwargs):
        self.build_kwargs = kwargs
        return self.context

    def enqueue_writeback(self, **kwargs):
        self.enqueued.append(kwargs)
        task_repo = kwargs["task_repo"]
        return task_repo.enqueue(
            kind="memory_writeback", payload={"x": 1}, idempotency_key="k"
        )


class _FakeMessageRepo:
    def __init__(self) -> None:
        self.appended: list[dict] = []
        self.conversations = None  # the route checks ownership before the turn runs

    def append(self, **kwargs):
        from ragos.storage.models.app import MessageIndex

        self.appended.append(kwargs)
        return MessageIndex(
            id=uuid4(),
            tenant_id=TENANT_ID,
            conversation_id=kwargs["conversation_id"],
            role=kwargs["role"],
            content=kwargs["content"],
            run_id=kwargs.get("run_id"),
            citations=list(kwargs.get("citations") or []),
        )

    def list_for_conversation(self, **kwargs):
        return []


class _FakeTaskRepo:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs):
        from uuid import uuid4 as _uuid4

        from ragos.storage.models.app import Task

        self.enqueued.append(kwargs)
        return Task(
            id=_uuid4(), tenant_id=TENANT_ID, kind=kwargs["kind"], payload=kwargs["payload"],
            idempotency_key=kwargs["idempotency_key"],
        )


def _service(tmp_path, *, result=None, facts=None, user_id=USER_ID):
    retrieval = _FakeRetrieval(result or _result())
    memory = _FakeMemoryManager(facts=facts)
    messages = _FakeMessageRepo()
    tasks = _FakeTaskRepo()
    sink = EventSink(EventLog(tmp_path), scope_key="scope", run_id=uuid4())

    service = ChatService(
        session=None,
        tenant_ctx=_ctx(user_id=user_id),
        deps=ChatDependencies(
            retrieval=retrieval,
            memory=memory,
            sink=sink,
            messages=messages,
            tasks=tasks,
            retriever_profile_id=uuid4(),
            window_n=6,
            memory_profile_version="mpv-1",
        ),
    )
    return service, retrieval, memory, messages, tasks


def _run(service) -> list:
    async def scenario():
        await service.run_turn(
            conversation_id=CONVERSATION_ID, user_message="what was revenue?"
        )
        return [event async for event in service.deps.sink.stream()]

    return asyncio.run(scenario())


def _names(events) -> list[str]:
    return [e.event for e in events]


def _collapse(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if not out or out[-1] != name:
            out.append(name)
    return out


# -- the turn ---------------------------------------------------------------


def test_events_arrive_in_the_documented_order(tmp_path) -> None:
    """Consecutive deltas collapse to one phase; their repetition is the stream."""
    service, *_ = _service(tmp_path)

    events = _run(service)

    assert _collapse(_names(events)) == [
        "run_started",
        "retrieval_completed",
        "rerank_completed",
        "relevance_graded",
        "token_delta",
        "citation_linked",
        "memory_writeback_scheduled",
        "run_finished",
    ]


def test_event_ids_are_monotonic_across_the_whole_turn(tmp_path) -> None:
    service, *_ = _service(tmp_path)

    events = _run(service)

    assert [e.event_id for e in events] == list(range(1, len(events) + 1))


def test_the_turn_refuses_without_a_resolvable_actor(tmp_path) -> None:
    """Serving the tenant's memory to one end user is a disclosure."""
    service, *_ = _service(tmp_path, user_id=None)

    with pytest.raises(PermissionDeniedError, match="user_id"):
        _run(service)


def test_an_empty_message_is_refused_before_any_event(tmp_path) -> None:
    service, *_ = _service(tmp_path)

    async def scenario():
        await service.run_turn(conversation_id=CONVERSATION_ID, user_message="   ")

    with pytest.raises(ValidationError, match="must not be empty"):
        asyncio.run(scenario())


def test_both_messages_are_persisted_with_their_citations(tmp_path) -> None:
    service, _r, _m, messages, _t = _service(tmp_path)

    _run(service)

    roles = [a["role"] for a in messages.appended]
    assert roles == ["user", "assistant"]
    assistant = messages.appended[1]
    assert assistant["citations"][0]["page_no"] == 3


def test_the_writeback_is_scheduled_with_the_recorded_window(tmp_path) -> None:
    service, _r, _m, _msgs, _t = _service(tmp_path)

    _run(service)

    enqueued = _m.enqueued[0]
    assert enqueued["window_n"] == 6
    assert enqueued["profile_version"] == "mpv-1"
    assert enqueued["user_id"] == USER_ID


def test_there_is_no_memory_updated_event(tmp_path) -> None:
    """The writeback is asynchronous; claiming it happened would be a lie."""
    service, *_ = _service(tmp_path)

    events = _run(service)

    assert "memory_updated" not in _names(events)
    assert "memory_writeback_scheduled" in _names(events)


def test_an_abstention_still_reports_the_full_trace(tmp_path) -> None:
    service, *_ = _service(tmp_path, result=_result(answer="I don't know.", abstained=True))

    events = _run(service)
    finished = next(e for e in events if e.event == "run_finished")

    assert finished.data["abstained"] is True
    linked = next(e for e in events if e.event == "citation_linked")
    assert linked.data["citations"] == []
    graded = next(e for e in events if e.event == "relevance_graded")
    assert graded.data["verdict"] == "irrelevant"


def test_the_retrieval_event_carries_the_per_channel_counts(tmp_path) -> None:
    service, *_ = _service(tmp_path)

    events = _run(service)
    retrieval = next(e for e in events if e.event == "retrieval_completed")

    assert retrieval.data["per_channel_counts"] == {"lexical": 1, "memory": 2}


def test_the_rerank_event_reports_parent_expansion(tmp_path) -> None:
    service, *_ = _service(tmp_path)

    events = _run(service)
    rerank = next(e for e in events if e.event == "rerank_completed")

    assert rerank.data["parents_resolved"] == 1


def test_memory_context_is_requested_for_this_actor(tmp_path) -> None:
    service, _r, memory, _m, _t = _service(tmp_path)

    _run(service)

    assert memory.build_kwargs["user_id"] == USER_ID
    assert memory.build_kwargs["query"] == "what was revenue?"


def test_a_memory_loaded_event_is_emitted_only_when_there_is_memory(tmp_path) -> None:
    from ragos.memory.types import FactCategory, MemoryFact

    plain, *_ = _service(tmp_path / "plain")
    with_memory, *_ = _service(
        tmp_path / "with",
        facts=[
            MemoryFact(
                id=uuid4(),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                fact="uses metric units",
                category=FactCategory.PREFERENCE.value,
                confidence=0.9,
            )
        ],
    )

    assert "memory_loaded" not in _names(_run(plain))
    loaded = next(e for e in _run(with_memory) if e.event == "memory_loaded")
    assert loaded.data["facts"] == 1


def test_the_pinned_profile_version_is_passed_to_retrieval(tmp_path) -> None:
    service, retrieval, *_ = _service(tmp_path)
    service.deps.retriever_profile_version = 4

    _run(service)

    assert retrieval.calls[0]["profile_version"] == 4


# -- segmentation -----------------------------------------------------------


def test_an_answer_is_delivered_as_multiple_deltas(tmp_path) -> None:
    service, *_ = _service(tmp_path, result=_result(answer="one two three [1]."))

    events = _run(service)
    deltas = [e.data["delta"] for e in events if e.event == "token_delta"]

    assert len(deltas) > 1
    assert "".join(deltas) == "one two three [1]."


def test_segmentation_is_cjk_aware() -> None:
    """Chinese has no whitespace to split on; one giant delta is not streaming."""
    assert _segment("中文测试文本") == ["中", "文", "测", "试", "文", "本"]


def test_a_pathological_token_is_bounded() -> None:
    deltas = _segment("x" * 1000)

    assert all(len(d) <= 240 for d in deltas)
    assert "".join(deltas) == "x" * 1000


def test_an_empty_answer_produces_no_deltas(tmp_path) -> None:
    service, *_ = _service(tmp_path, result=_result(answer="", abstained=True))

    events = _run(service)

    assert [e for e in events if e.event == "token_delta"] == []
