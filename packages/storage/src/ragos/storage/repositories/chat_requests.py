"""PostgreSQL-authoritative chat idempotency, scoped to an authenticated actor.

The unique index arbitrates racing POST requests across workers. A failed or
lost model call is NOT re-executed automatically; only an operator-controlled
reconciliation may act on an ambiguous claim.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from ragos.core.errors import ConflictError, NotFoundError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import ChatRequest
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from ragos.storage.repositories.conversations import ConversationRepository
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{12,128}\Z", re.ASCII)
_SHA_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class ChatRequestRepository(TenantRepository[ChatRequest]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_private_scope_key()
        tenant_ctx.require_user_id()
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=ChatRequest)
        self.conversations = ConversationRepository(session, tenant_ctx)

    def claim(
        self, *, conversation_id: UUID, idempotency_key: str, request_digest: str
    ) -> tuple[ChatRequest, bool]:
        """Atomic claim before any billable work: (persisted request, new owner).

        A conflicting insert waits for the winner's unique-index transaction
        to commit, then reads the winner's record. Never relies on an in-memory
        lock or SELECT-then-INSERT check.
        """
        if not isinstance(idempotency_key, str) or not _KEY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise ValidationError("Idempotency-Key must be 12-128 URL-safe characters")
        if not isinstance(request_digest, str) or not _SHA_PATTERN.fullmatch(
            request_digest
        ):
            raise ValidationError("request fingerprint must be a sha256 hex digest")
        self.conversations.require_by_id(conversation_id)
        user_id = self.tenant_ctx.require_user_id()
        request_id = uuid4()
        now = utc_now()
        statement = (
            pg_insert(ChatRequest)
            .values(
                id=request_id,
                tenant_id=self.tenant_ctx.tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                status="claimed",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_chat_request_actor_key")
            .returning(ChatRequest.id)
        )
        created = self.session.execute(statement).scalar_one_or_none()
        if created is not None:
            record = self.require_by_id(created)
            return record, True

        record = self.session.exec(
            self.scoped_statement()
            .where(ChatRequest.user_id == user_id)
            .where(ChatRequest.conversation_id == conversation_id)
            .where(ChatRequest.idempotency_key == idempotency_key)
        ).first()
        if record is None:
            raise NotFoundError("Chat request claim not found")
        if record.request_digest != request_digest:
            raise ConflictError(
                "Idempotency-Key is already bound to a different request"
            )
        return record, False

    def _lock_actor_run(self, *, conversation_id: UUID, run_id: UUID) -> ChatRequest:
        self.conversations.require_by_id(conversation_id)
        request = self.session.exec(
            self.scoped_statement()
            .where(ChatRequest.id == run_id)
            .where(ChatRequest.user_id == self.tenant_ctx.require_user_id())
            .where(ChatRequest.conversation_id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if request is None:
            raise NotFoundError("Chat request run not found")
        return request

    def complete(self, *, conversation_id: UUID, run_id: UUID) -> ChatRequest:
        """Call in the SAME SQL transaction as the assistant message and task."""
        request = self._lock_actor_run(conversation_id=conversation_id, run_id=run_id)
        if request.status != "claimed":
            raise ConflictError("Chat request run cannot complete twice")
        request.status = "completed"
        request.finished_at = utc_now()
        request.updated_at = request.finished_at
        self.session.add(request)
        self.session.flush()
        return request

    def mark_failed(self, *, conversation_id: UUID, run_id: UUID) -> ChatRequest:
        """Terminal-fail only uncompleted claims, never erase committed success."""
        request = self._lock_actor_run(conversation_id=conversation_id, run_id=run_id)
        if request.status == "claimed":
            request.status = "failed"
            request.finished_at = utc_now()
            request.updated_at = request.finished_at
            self.session.add(request)
            self.session.flush()
        return request
