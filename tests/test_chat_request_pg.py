"""Real PostgreSQL durable chat claim, uniqueness, tenant and actor isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError, NotFoundError
from ragos.storage.models.app import ChatRequest, User
from ragos.storage.repositories.chat_requests import ChatRequestRepository
from ragos.storage.repositories.conversations import ConversationRepository
from test_storage_integration import context, database, resources, scoped_session

__all__ = ["database"]

DIGEST = "a" * 64


def _setup(engine):
    tenant_id, kb_id, _ = resources(engine)
    user_id = uuid4()
    ctx = context(tenant_id, kb_id, user_id=user_id)
    with scoped_session(engine, ctx) as session:
        session.add(
            User(
                id=user_id, tenant_id=tenant_id, external_id=f"chat-{uuid4()}",
                display_name="Synthetic chat user",
            )
        )
        session.flush()
        conversation = ConversationRepository(session, ctx).create(
            scope_key=ctx.require_private_scope_key(),
            user_id=user_id,
            title="Synthetic",
        )
        conversation_id = conversation.id
    return ctx, conversation_id


def test_claim_frozen_key_payload_and_terminal_states(database) -> None:
    ctx, conv = _setup(database)
    with scoped_session(database, ctx) as session:
        repo = ChatRequestRepository(session, ctx)
        claimed, created = repo.claim(
            conversation_id=conv, idempotency_key="chat-key-0000000001",
            request_digest=DIGEST,
        )
        assert created and claimed.status == "claimed"
        run_id = claimed.id
        persisted = session.get(ChatRequest, run_id)
        assert persisted.request_digest == DIGEST
        assert not hasattr(persisted, "message")

    with scoped_session(database, ctx) as session:
        repo = ChatRequestRepository(session, ctx)
        duplicate, created = repo.claim(
            conversation_id=conv, idempotency_key="chat-key-0000000001",
            request_digest=DIGEST,
        )
        assert not created and duplicate.id == run_id
        with pytest.raises(ConflictError, match="different request"):
            repo.claim(
                conversation_id=conv, idempotency_key="chat-key-0000000001",
                request_digest="b" * 64,
            )
        repo.complete(conversation_id=conv, run_id=run_id)
        assert repo.mark_failed(conversation_id=conv, run_id=run_id).status == "completed"

    with scoped_session(database, ctx) as session:
        repo = ChatRequestRepository(session, ctx)
        duplicate, created = repo.claim(
            conversation_id=conv, idempotency_key="chat-key-0000000001",
            request_digest=DIGEST,
        )
        assert not created and duplicate.status == "completed"
        with pytest.raises(ConflictError, match="cannot complete"):
            repo.complete(conversation_id=conv, run_id=run_id)

        second, created = repo.claim(
            conversation_id=conv, idempotency_key="chat-key-0000000002",
            request_digest=DIGEST,
        )
        assert created
        assert repo.mark_failed(conversation_id=conv, run_id=second.id).status == "failed"
        assert repo.claim(
            conversation_id=conv, idempotency_key="chat-key-0000000002",
            request_digest=DIGEST,
        )[0].status == "failed"


def test_other_actor_cannot_claim_another_users_conversation(database) -> None:
    ctx, conversation_id = _setup(database)
    other = context(ctx.tenant_id, ctx.allowed_kb_ids[0], user_id=uuid4())
    with scoped_session(database, other) as session:
        repo = ChatRequestRepository(session, other)
        with pytest.raises(NotFoundError, match="Conversation"):
            repo.claim(
                conversation_id=conversation_id,
                idempotency_key="another-actor-key",
                request_digest=DIGEST,
            )


def test_db_unique_constraint_admits_exactly_one_concurrent_claim(database) -> None:
    ctx, conversation_id = _setup(database)
    barrier = Barrier(2)
    key = "concurrent-request-key"

    def worker():
        with scoped_session(database, ctx) as session:
            barrier.wait(timeout=8)
            record, created = ChatRequestRepository(session, ctx).claim(
                conversation_id=conversation_id, idempotency_key=key,
                request_digest=DIGEST,
            )
            return str(record.id), created

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        observations = [future.result(timeout=15) for future in futures]
    assert len({record_id for record_id, _ in observations}) == 1
    assert sum(created for _, created in observations) == 1
