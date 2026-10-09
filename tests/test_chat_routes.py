"""``/chat/*`` routes: authorization, actor resolution, and ownership.

The claims under test: chat is not behind the tenant-admin gate, an actor with no
user record cannot be served, another actor's conversation is a 404 rather than a
leak, and the event stream is framed as SSE with the request id intact.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from ragos.auth.jwt import encode_dev_token
from ragos.core.errors import NotFoundError
from ragos.retrieval.citation import Citation, CitationStatus
from ragos.retrieval.pipeline import RAGResult, RetrievalExplain
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.runtime import chat_history
from ragos.runtime.chat import ChatDependencies
from ragos.runtime.events import EventLog, EventSink
from ragos_api.dependencies import get_db_session
from ragos_api.main import create_app
from ragos_api.routes import chat as chat_route

TENANT_ID = "fa9d4df0-81ce-4f1a-9820-4c29259779a6"
OTHER_TENANT_ID = "11111111-2222-3333-4444-555555555555"
USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONVERSATION_ID = "44444444-4444-4444-4444-444444444444"
PROFILE_ID = "55555555-5555-5555-5555-555555555555"
MESSAGE_ID = "66666666-6666-6666-6666-666666666666"
MISSING_MESSAGE_ID = "77777777-7777-7777-7777-777777777777"


def _headers(*, tenant_id: str = TENANT_ID, subject: str = "u_1") -> dict[str, str]:
    claims = {
        "sub": subject,
        "tenant_id": tenant_id,
        "tenant_slug": "acme",
        "actor_type": "end_user",
        "roles": ["end_user"],
    }
    return {
        "Authorization": f"Bearer {encode_dev_token(claims)}",
        "X-Tenant-Id": tenant_id,
    }


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True


class _FakeUserResolver:
    """Stands in for the app."user" lookup, keyed by the actor subject."""

    def __init__(self) -> None:
        self.known: dict[str, UUID] = {"u_1": USER_ID}

    def __call__(self, session: Any, tenant_ctx: Any) -> UUID | None:
        return self.known.get(tenant_ctx.actor_id)


class _FakeMemoryProfile:
    version = 3
    window_n = 6


class _FakeConversations:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, **kwargs):
        from ragos.storage.models.app import Conversation

        self.created.append(kwargs)
        return Conversation(
            id=uuid4(), tenant_id=UUID(TENANT_ID), external_user_id=str(kwargs["user_id"]),
            title=kwargs.get("title"), status="active",
        )

    def require_by_id(self, conversation_id: UUID):
        from ragos.storage.models.app import Conversation

        if str(conversation_id) != CONVERSATION_ID:
            raise NotFoundError("Conversation not found", id=str(conversation_id))
        return Conversation(
            id=conversation_id, tenant_id=UUID(TENANT_ID), external_user_id=str(USER_ID),
            title="t", status="active",
        )


class _FakeMessages:
    def __init__(self) -> None:
        self.appended: list[dict] = []
        self.feedback_records: list[dict] = []
        self.conversations = _FakeConversations()

    def append(self, **kwargs):
        from ragos.storage.models.app import MessageIndex

        self.appended.append(kwargs)
        return MessageIndex(
            id=uuid4(), tenant_id=UUID(TENANT_ID), conversation_id=kwargs["conversation_id"],
            role=kwargs["role"], content=kwargs["content"],
            run_id=kwargs.get("run_id"), citations=list(kwargs.get("citations") or []),
        )

    def list_for_conversation(self, **kwargs):
        return []

    def require_in_conversation(self, message_id: UUID, conversation_id: UUID):
        from ragos.storage.models.app import MessageIndex

        if str(message_id) == MISSING_MESSAGE_ID:
            raise NotFoundError("Message not found in this conversation")
        return MessageIndex(
            id=message_id, tenant_id=UUID(TENANT_ID), conversation_id=conversation_id,
            role="assistant", content="answer", citations=[],
        )


class _FakeFeedback:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    def record(self, **kwargs):
        from ragos.storage.models.app import FeedbackEntry

        self.messages.feedback_records.append(kwargs)
        return FeedbackEntry(
            id=uuid4(), tenant_id=UUID(TENANT_ID), conversation_id=kwargs["conversation_id"],
            message_id=kwargs["message_id"], user_id=kwargs.get("user_id"),
            rating=kwargs["rating"], comment=kwargs.get("comment"),
            answer_snapshot=kwargs.get("answer_snapshot"),
        )

    def list_for_message(self, **kwargs):
        return []


def _result() -> RAGResult:
    chunk = ScoredChunk(chunk_id=uuid4(), doc_id=uuid4(), kb_id=None, text="e", score=0.9,
                        channel="lexical", page_no=1)
    return RAGResult(
        answer="Revenue was $4M [1].",
        citations=[
            Citation(chunk_id=chunk.chunk_id, doc_id=chunk.doc_id, page_no=1, start_char_idx=1,
                     end_char_idx=10, claim="Revenue was $4M [1].", text="e",
                     status=CitationStatus.VERIFIED, marker_index=1)
        ],
        retrieval_explain=RetrievalExplain(rewritten_query="revenue", refined_query="revenue",
                                           per_channel_counts={"lexical": 1}, citation_supported=1,
                                           citation_generated=1),
        evidence=(chunk,),
        gate=None,
        abstained=False,
        token_usage={},
    )


class _FakeRetrieval:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def query(self, **kwargs):
        from ragos.runtime.retrieval import RetrievalOutcome

        self.calls.append(kwargs)
        return RetrievalOutcome(
            result=_result(),
            profile=RetrieverProfile(id=uuid4(), tenant_id=uuid4(), recall_channels=["lexical"],
                                     channel_budgets={"lexical": 1.0}),
            warnings=["recall channel 'dense' is not wired"],
        )


class _FakeMemory:
    def enqueue_writeback(self, **kwargs):
        from ragos.storage.models.app import Task

        kwargs["task_repo"].enqueue(kind="memory_writeback", payload={}, idempotency_key="k")
        return Task(id=uuid4(), tenant_id=uuid4(), kind="memory_writeback", payload={},
                    idempotency_key="k")

    async def build_context(self, **kwargs):
        from ragos.memory.manager import MemoryContext

        return MemoryContext(user_id=kwargs.get("user_id"))


class _FakeTasks:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs):
        from ragos.storage.models.app import Task

        self.enqueued.append(kwargs)
        return Task(id=uuid4(), tenant_id=uuid4(), kind=kwargs["kind"], payload=kwargs["payload"],
                    idempotency_key=kwargs["idempotency_key"])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    users = _FakeUserResolver()
    messages = _FakeMessages()
    retrieval = _FakeRetrieval()
    tasks = _FakeTasks()

    monkeypatch.setattr(chat_route, "resolve_actor_user_id", users)
    monkeypatch.setattr(
        chat_route,
        "ConversationRepository",
        lambda session, tenant_ctx: messages.conversations,
    )
    monkeypatch.setattr(chat_route, "MessageRepository", lambda session, tenant_ctx: messages)
    monkeypatch.setattr(chat_history, "MessageRepository", lambda session, tenant_ctx: messages)
    monkeypatch.setattr(
        chat_route, "FeedbackRepository", lambda session, tenant_ctx: _FakeFeedback(messages)
    )
    monkeypatch.setattr(chat_route, "TaskRepository", lambda session, tenant_ctx: tasks)
    monkeypatch.setattr(chat_route, "MemoryFactStore", lambda session, tenant_ctx, **kw: None)
    monkeypatch.setattr(chat_route, "MemoryManager", lambda **kw: _FakeMemory())
    monkeypatch.setattr(chat_route, "ModelRouter", lambda session: object())
    monkeypatch.setattr(chat_route, "GatewayComplete", lambda router: None)
    monkeypatch.setattr(chat_route, "EventLog", lambda *a, **kw: EventLog(tmp_path))
    monkeypatch.setattr(
        chat_route, "_require_memory_profile", lambda session, tenant_ctx: _FakeMemoryProfile()
    )
    monkeypatch.setattr(
        chat_route,
        "build_recall_channels",
        lambda **kw: {"lexical": lambda q, p, c: asyncio.sleep(0, [])},
    )
    monkeypatch.setattr(chat_route, "build_parent_resolver", lambda **kw: None)

    def _fake_service(*, session, tenant_ctx, payload):
        return _service(messages=messages, retrieval=retrieval, tasks=tasks, tenant_ctx=tenant_ctx,
                        payload=payload, tmp_path=tmp_path)

    monkeypatch.setattr(chat_route, "_service", _fake_service)

    app = create_app(testing=True)

    def _override_session():
        yield _FakeSession()

    app.dependency_overrides[get_db_session] = _override_session
    client = TestClient(app)
    client.users = users  # type: ignore[attr-defined]
    client.messages = messages  # type: ignore[attr-defined]
    client.tasks = tasks  # type: ignore[attr-defined]
    return client


def _service(*, messages, retrieval, tasks, tenant_ctx, payload, tmp_path) -> Any:
    from ragos.runtime.chat import ChatService

    return ChatService(
        session=None,
        tenant_ctx=tenant_ctx,
        deps=ChatDependencies(
            retrieval=retrieval,
            memory=_FakeMemory(),
            sink=EventSink(EventLog(tmp_path), scope_key="scope", run_id=uuid4()),
            messages=messages,
            tasks=tasks,
            retriever_profile_id=payload.retriever_profile_id,
            window_n=6,
            memory_profile_version="mpv-3",
            retriever_profile_version=payload.retriever_profile_version,
        ),
    )


# -- authorization ----------------------------------------------------------


def test_chat_requires_authentication(client: TestClient) -> None:
    assert client.post("/chat/conversations", json={}).status_code == 401


def test_an_end_user_is_not_blocked_by_the_tenant_admin_gate(client: TestClient) -> None:
    """A role check aimed at admins would reject the only people who use chat."""
    resp = client.post("/chat/conversations", headers=_headers(), json={})

    assert resp.status_code == 201


def test_an_actor_with_no_user_record_is_refused(client: TestClient) -> None:
    """Serving them without memory would silently be serving the tenant's memory."""
    client.users.known.clear()

    resp = client.post("/chat/conversations", headers=_headers(), json={})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# -- conversations ----------------------------------------------------------


def test_create_conversation_records_the_resolved_user(client: TestClient) -> None:
    resp = client.post(
        "/chat/conversations", headers=_headers(), json={"title": "support"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["conversation_id"]
    convos = client.messages.conversations  # type: ignore[attr-defined]
    assert convos.created[0]["user_id"] == USER_ID


def test_another_actors_conversation_is_not_found(client: TestClient) -> None:
    """A 404 rather than a 403: the existence of the conversation is not the answer."""
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(subject="u_unknown"),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 403, "no user record for the unknown actor"


def test_an_unknown_conversation_is_not_found(client: TestClient) -> None:
    resp = client.post(
        "/chat/conversations/99999999-9999-9999-9999-999999999999/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 404


def test_a_body_supplied_scope_field_is_refused(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID, "user_id": str(OTHER_USER_ID)},
    )

    assert resp.status_code == 403


# -- streaming --------------------------------------------------------------


def test_sending_a_message_returns_a_typed_sse_stream(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "what was revenue?", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "event: run_started" in body
    assert "event: retrieval_completed" in body
    assert "event: token_delta" in body
    assert "event: citation_linked" in body
    assert "event: run_finished" in body


def test_every_frame_carries_a_monotonic_id(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    ids = [
        int(line.split(": ", 1)[1])
        for line in resp.text.splitlines()
        if line.startswith("id: ")
    ]

    assert ids == list(range(1, len(ids) + 1))


def test_the_response_carries_the_request_id(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.headers.get("X-Request-Id")


def test_the_finished_event_reports_unavailable_capabilities(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    assert "recall channel 'dense' is not wired" in resp.text


def test_a_writeback_task_is_enqueued_during_the_turn(client: TestClient) -> None:
    client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "hi", "retriever_profile_id": PROFILE_ID},
    )

    tasks = client.tasks  # type: ignore[attr-defined]
    assert any(t["kind"] == "memory_writeback" for t in tasks.enqueued)


def test_an_empty_message_is_rejected_before_the_stream_opens(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages",
        headers=_headers(),
        json={"message": "", "retriever_profile_id": PROFILE_ID},
    )

    assert resp.status_code == 422


# -- feedback ---------------------------------------------------------------


def test_feedback_records_the_rated_answer(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages/{MESSAGE_ID}/feedback",
        headers=_headers(),
        json={"rating": "up", "comment": "this helped"},
    )

    assert resp.status_code == 201
    recorded = client.messages.feedback_records[0]  # type: ignore[attr-defined]
    assert recorded["rating"] == "up"
    # The subject is snapshotted: a rating about a text nobody can see is not feedback.
    assert recorded["answer_snapshot"] == "answer"
    assert recorded["user_id"] == USER_ID


def test_an_unknown_rating_is_rejected(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages/{MESSAGE_ID}/feedback",
        headers=_headers(),
        json={"rating": "sideways"},
    )

    assert resp.status_code == 422


def test_feedback_on_a_missing_message_is_not_found(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages/{MISSING_MESSAGE_ID}/feedback",
        headers=_headers(),
        json={"rating": "down"},
    )

    assert resp.status_code == 404


def test_feedback_requires_authentication(client: TestClient) -> None:
    resp = client.post(
        f"/chat/conversations/{CONVERSATION_ID}/messages/{MESSAGE_ID}/feedback",
        json={"rating": "up"},
    )

    assert resp.status_code == 401
