"""Conversations, messages, and feedback.

Actor-scoped, like every tenant-scoped read here. ``user_id`` is a required
parameter rather than an optional filter: a conversation belongs to one user, so
listing "all conversations in this tenant" is not a wider search, it is a different
and much larger dataset that no caller of this repository has asked for.

``scope_key`` is required and checked on every method, for the reason the other
repositories share — a repository that resolves its own scope can be widened by a
caller reaching into the context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from ragos.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import Conversation, FeedbackEntry, MessageIndex
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from sqlmodel import Session

#: Ratings a caller may submit.
RATINGS = ("up", "down")


class ConversationRepository(TenantRepository[Conversation]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_private_scope_key()
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=Conversation)
        self.scope_key = tenant_ctx.require_private_scope_key()

    def _require_scope_key(self, scope_key: str) -> None:
        if not scope_key:
            raise PermissionDeniedError(
                "scope_key is required and must not be empty",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if scope_key != self.scope_key:
            raise PermissionDeniedError(
                "scope_key does not match the resolved tenant scope",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )

    def create(self, *, scope_key: str, user_id: UUID, title: str | None = None) -> Conversation:
        self._require_scope_key(scope_key)
        if user_id != self.tenant_ctx.require_user_id():
            raise PermissionDeniedError("conversation owner does not match")
        conversation = Conversation(
            tenant_id=self.tenant_ctx.tenant_id,
            external_user_id=str(user_id),
            title=title,
            status="active",
        )
        self.session.add(conversation)
        self.session.flush()
        self.session.refresh(conversation)
        return conversation

    def require_by_id(self, conversation_id: UUID) -> Conversation:
        """Unlike the generic lookup, this is actor-scoped.

        A conversation id arriving from a URL is a caller-supplied identifier, and
        the response for someone else's conversation would otherwise be a 200 with
        their title in it.
        """
        conversation = self.get_by_id(conversation_id)
        if conversation is None:
            raise NotFoundError(
                "Conversation not found",
                resource="Conversation",
                id=str(conversation_id),
            )
        if conversation.external_user_id != str(self.tenant_ctx.require_user_id()):
            raise NotFoundError(
                "Conversation not found",
                resource="Conversation",
                id=str(conversation_id),
            )
        return conversation


class MessageRepository(TenantRepository[MessageIndex]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_private_scope_key()
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=MessageIndex)
        self.scope_key = tenant_ctx.require_private_scope_key()
        self.conversations = ConversationRepository(session, tenant_ctx)

    def _require_scope_key(self, scope_key: str) -> None:
        if not scope_key:
            raise PermissionDeniedError(
                "scope_key is required and must not be empty",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if scope_key != self.scope_key:
            raise PermissionDeniedError(
                "scope_key does not match the resolved tenant scope",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )

    def append(
        self,
        *,
        scope_key: str,
        conversation_id: UUID,
        role: str,
        content: str,
        run_id: UUID | None = None,
        citations: list[dict[str, Any]] | None = None,
        token_count: int | None = None,
        is_summary: bool = False,
    ) -> MessageIndex:
        self._require_scope_key(scope_key)
        # Confirms the conversation belongs to this actor before anything is written
        # under it.
        self.conversations.require_by_id(conversation_id)

        message = MessageIndex(
            tenant_id=self.tenant_ctx.tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            run_id=run_id,
            citations=list(citations or []),
            token_count=token_count,
            is_summary=is_summary,
        )
        self.session.add(message)
        self.session.flush()
        self.session.refresh(message)
        return message

    def list_for_conversation(
        self, *, scope_key: str, conversation_id: UUID, limit: int = 200
    ) -> list[MessageIndex]:
        self._require_scope_key(scope_key)
        self.conversations.require_by_id(conversation_id)
        statement = (
            self.scoped_statement()
            .where(MessageIndex.conversation_id == conversation_id)
            .order_by(MessageIndex.created_at, MessageIndex.id)
            .limit(limit)
        )
        return list(self.session.exec(statement).all())

    def require_completed_run(
        self, *, conversation_id: UUID, run_id: UUID
    ) -> MessageIndex:
        """Authorize a replay by a committed assistant message of this actor.

        Use a scoped SQL query rather than the paginated conversation history,
        so older completed runs remain accessible. Unknown, unfinished and
        foreign-actor runs are indistinguishable (404).
        """
        self.conversations.require_by_id(conversation_id)
        statement = (
            self.scoped_statement()
            .where(MessageIndex.conversation_id == conversation_id)
            .where(MessageIndex.run_id == run_id)
            .where(MessageIndex.role == "assistant")
            .limit(1)
        )
        message = self.session.exec(statement).first()
        if message is None:
            raise NotFoundError("Completed chat run not found")
        return message

    def require_in_conversation(self, message_id: UUID, conversation_id: UUID) -> MessageIndex:
        for message in self.list_for_conversation(
            scope_key=self.scope_key, conversation_id=conversation_id
        ):
            if message.id == message_id:
                return message
        raise NotFoundError(
            "Message not found in this conversation",
            resource="MessageIndex",
            id=str(message_id),
            conversation_id=str(conversation_id),
        )


class FeedbackRepository(TenantRepository[FeedbackEntry]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_private_scope_key()
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=FeedbackEntry)
        self.scope_key = tenant_ctx.require_private_scope_key()

    def _require_scope_key(self, scope_key: str) -> None:
        if not scope_key:
            raise PermissionDeniedError(
                "scope_key is required and must not be empty",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if scope_key != self.scope_key:
            raise PermissionDeniedError(
                "scope_key does not match the resolved tenant scope",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )

    def record(
        self,
        *,
        scope_key: str,
        conversation_id: UUID,
        message_id: UUID,
        rating: str,
        user_id: UUID | None = None,
        run_id: UUID | None = None,
        comment: str | None = None,
        answer_snapshot: str | None = None,
    ) -> FeedbackEntry:
        """Record or update one actor's rating of one message.

        A re-rating updates the row: two contradictory ratings on the same answer
        make the eval dataset ambiguous, and "changed their mind" is recorded by the
        updated_at on a single row rather than by a second opinion.
        """
        self._require_scope_key(scope_key)
        if rating not in RATINGS:
            raise ConflictError("Unsupported rating", rating=rating, supported=list(RATINGS))

        owner = user_id or self.tenant_ctx.require_user_id()
        ConversationRepository(self.session, self.tenant_ctx).require_by_id(conversation_id)
        if owner != self.tenant_ctx.require_user_id():
            raise PermissionDeniedError("feedback owner does not match")
        statement = (
            self.scoped_statement()
            .where(FeedbackEntry.message_id == message_id)
            .where(FeedbackEntry.user_id == owner)
        )
        existing = self.session.exec(statement).first()
        if existing is not None:
            existing.rating = rating
            existing.comment = comment
            existing.run_id = run_id
            existing.answer_snapshot = answer_snapshot
            existing.updated_at = utc_now()
            self.session.add(existing)
            self.session.flush()
            self.session.refresh(existing)
            return existing

        entry = FeedbackEntry(
            id=uuid4(),
            tenant_id=self.tenant_ctx.tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=owner,
            run_id=run_id,
            rating=rating,
            comment=comment,
            answer_snapshot=answer_snapshot,
        )
        self.session.add(entry)
        self.session.flush()
        self.session.refresh(entry)
        return entry

    def list_for_message(self, *, scope_key: str, message_id: UUID) -> list[FeedbackEntry]:
        self._require_scope_key(scope_key)
        statement = (
            self.scoped_statement()
            .where(FeedbackEntry.message_id == message_id)
            .where(FeedbackEntry.user_id == self.tenant_ctx.require_user_id())
            .order_by(FeedbackEntry.created_at)
        )
        return list(self.session.exec(statement).all())


def recent_turns(messages: list[MessageIndex], *, limit: int) -> list[tuple[str, int, str]]:
    """Project stored messages into ``(role, index, text)`` for the summarizer."""
    return [
        (m.role, idx, m.content)
        for idx, m in enumerate(messages[-limit:], start=max(0, len(messages) - limit))
    ]


def message_created_at(message: MessageIndex) -> datetime:
    return message.created_at
