"""``/chat/*`` — the end-user surface.

Different authorization from ``/tenant/*`` on purpose: a chat actor is an end user,
and the tenant-admin gate would reject exactly the people this endpoint exists for.
The gate here is *authenticated plus a resolvable user record* — memory is
per-actor, so an actor with no ``app.user`` row cannot be served: answering them
without memory and answering them with the tenant's memory are different products,
and only one of them was asked for.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from ragos.core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ragos.core.tenant import TenantContext
from ragos.memory.fact_store import MemoryFactStore
from ragos.memory.manager import MemoryManager
from ragos.model_gateway.router import ModelRouter
from ragos.retrieval.gateway import GatewayComplete
from ragos.runtime.chat import ChatDependencies, ChatService
from ragos.runtime.events import EventLog, EventSink, parse_last_event_id
from ragos.runtime.retrieval import (
    RetrievalService,
    build_parent_resolver,
    build_recall_channels,
)
from ragos.storage.repositories.conversations import (
    ConversationRepository,
    FeedbackRepository,
    MessageRepository,
)
from ragos.storage.repositories.tasks import TaskRepository
from ragos.tenant.resolver import resolve_actor_user_id
from sqlmodel import Session, select

from ragos_api.dependencies import DBSession, TenantCtx
from ragos_api.scope import enforce_body_scope

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    dependencies=[Depends(enforce_body_scope)],
)


def require_chat_actor(request: Request) -> None:
    """Authenticated, and on a path that resolved a tenant scope.

    Not ``require_tenant_admin``: an end user is the actor here, and a role check
    aimed at administrators would reject the only people who use chat.
    """
    if getattr(request.state, "auth_claims", None) is None:
        raise AuthenticationError()
    if getattr(request.state, "tenant_ctx", None) is None:
        raise ValidationError("chat requires a resolved tenant context")


def resolve_user(session: Session, tenant_ctx: TenantContext) -> UUID:
    """Attach the actor's user row to the context.

    Resolution is server-side from the verified subject, so there is no request
    value to forge. A missing row is a 403 rather than a silent tenant-wide memory
    read.
    """
    user_id = resolve_actor_user_id(session, tenant_ctx)
    if user_id is None:
        raise PermissionDeniedError(
            "no user record for this actor; refusing to serve chat without a "
            "per-actor memory scope",
            actor_id=tenant_ctx.actor_id,
        )
    return user_id


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    retriever_profile_id: UUID
    #: Pin a profile version when replaying a run rather than serving the live one.
    retriever_profile_version: int | None = Field(default=None, ge=1)


class FeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=2000)


def _require_memory_profile(session: Session, tenant_ctx: TenantContext):
    """Latest recorded memory profile for this tenant.

    ``window_n`` is an evaluated parameter, so it comes from the tenant's recorded
    profile. With none recorded the turn is refused: serving chat under an ``N``
    nobody chose is the heuristic the memory rules forbid.
    """
    from ragos.storage.models.app import MemoryProfileVersion

    statement = (
        select(MemoryProfileVersion)
        .where(MemoryProfileVersion.tenant_id == tenant_ctx.tenant_id)
        .order_by(MemoryProfileVersion.version.desc())
    )
    profile = session.exec(statement).first()
    if profile is None:
        raise NotFoundError(
            "No recorded memory profile for this tenant; record one with an "
            "evaluated summarization window before serving chat",
            resource="MemoryProfileVersion",
        )
    return profile


def _service(
    *,
    session: Session,
    tenant_ctx: TenantContext,
    payload: SendMessageRequest,
) -> ChatService:
    """Assemble one chat turn's dependencies.

    One gateway instance serves generation, grading, query planning, the query
    embedding, extraction, and summarization, so they share a key pool and one cost
    ledger.
    """
    memory_profile = _require_memory_profile(session, tenant_ctx)
    router = ModelRouter(session)
    sink = EventSink(
        EventLog(), scope_key=tenant_ctx.require_scope_key(), run_id=uuid4()
    )

    return ChatService(
        session=session,
        tenant_ctx=tenant_ctx,
        deps=ChatDependencies(
            retrieval=RetrievalService(
                session=session,
                tenant_ctx=tenant_ctx,
                recall_channels=build_recall_channels(
                    session=session, tenant_ctx=tenant_ctx, router=router
                ),
                parent_resolver=build_parent_resolver(session=session, tenant_ctx=tenant_ctx),
                router=router,
            ),
            memory=MemoryManager(
                fact_store=MemoryFactStore(session=session, tenant_ctx=tenant_ctx),
                complete=GatewayComplete(router),
            ),
            sink=sink,
            messages=MessageRepository(session, tenant_ctx),
            tasks=TaskRepository(session, tenant_ctx),
            retriever_profile_id=payload.retriever_profile_id,
            window_n=memory_profile.window_n,
            memory_profile_version=f"mpv-{memory_profile.version}",
            retriever_profile_version=payload.retriever_profile_version,
        ),
    )


@router.post("/conversations", status_code=201, dependencies=[Depends(require_chat_actor)])
def create_conversation(
    payload: CreateConversationRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    user_id = resolve_user(session, tenant_ctx)
    tenant_ctx.user_id = user_id

    conversation = ConversationRepository(session=session, tenant_ctx=tenant_ctx).create(
        scope_key=tenant_ctx.require_scope_key(),
        user_id=user_id,
        title=payload.title,
    )
    session.commit()
    return {
        "conversation_id": str(conversation.id),
        "title": conversation.title,
        "status": conversation.status,
    }


@router.post(
    "/conversations/{conversation_id}/messages", dependencies=[Depends(require_chat_actor)]
)
async def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Stream a turn as SSE.

    ``Last-Event-ID`` resumes: the sink replays from the run's JSONL log before
    following live events, so a reconnect neither loses an acknowledged event nor
    repeats one.
    """
    user_id = resolve_user(session, tenant_ctx)
    tenant_ctx.user_id = user_id

    service = _service(session=session, tenant_ctx=tenant_ctx, payload=payload)
    # Confirm the conversation belongs to this actor before any model call, so
    # another actor's id is a 404 rather than an expensive refusal.
    service.deps.messages.conversations.require_by_id(conversation_id)

    async def _produce() -> None:
        await service.run_turn(
            conversation_id=conversation_id, user_message=payload.message
        )
        session.commit()

    async def _stream():
        producer = asyncio.create_task(_produce())
        try:
            async for event in service.deps.sink.stream(
                last_event_id=parse_last_event_id(last_event_id)
            ):
                yield event.to_sse()
        finally:
            # The producer keeps writing to the run log even if the client is gone,
            # so the run stays resumable.
            await asyncio.shield(producer)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/conversations/{conversation_id}/messages", dependencies=[Depends(require_chat_actor)]
)
def list_messages(
    conversation_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = 50,
) -> dict[str, Any]:
    resolve_user(session, tenant_ctx)
    messages = MessageRepository(session=session, tenant_ctx=tenant_ctx).list_for_conversation(
        scope_key=tenant_ctx.require_scope_key(), conversation_id=conversation_id, limit=limit
    )
    return {
        "items": [
            {
                "message_id": str(m.id),
                "role": m.role,
                "content": m.content,
                "citations": list(m.citations or []),
                "run_id": str(m.run_id) if m.run_id else None,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]
    }


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    status_code=201,
    dependencies=[Depends(require_chat_actor)],
)
def submit_feedback(
    conversation_id: UUID,
    message_id: UUID,
    payload: FeedbackRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    user_id = resolve_user(session, tenant_ctx)
    tenant_ctx.user_id = user_id

    messages = MessageRepository(session=session, tenant_ctx=tenant_ctx)
    message = messages.require_in_conversation(message_id, conversation_id)

    entry = FeedbackRepository(session=session, tenant_ctx=tenant_ctx).record(
        scope_key=tenant_ctx.require_scope_key(),
        conversation_id=conversation_id,
        message_id=message_id,
        rating=payload.rating,
        user_id=user_id,
        run_id=message.run_id,
        answer_snapshot=message.content,
    )
    session.commit()
    return {"feedback_id": str(entry.id), "rating": entry.rating, "message_id": str(message_id)}


@router.get(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    dependencies=[Depends(require_chat_actor)],
)
def list_feedback(
    conversation_id: UUID,
    message_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    resolve_user(session, tenant_ctx)
    # Require the message to belong to this conversation first: a message id from
    # another conversation is not this caller's to read feedback on.
    MessageRepository(session=session, tenant_ctx=tenant_ctx).require_in_conversation(
        message_id, conversation_id
    )
    entries = FeedbackRepository(session=session, tenant_ctx=tenant_ctx).list_for_message(
        scope_key=tenant_ctx.require_scope_key(), message_id=message_id
    )
    return {
        "items": [
            {"feedback_id": str(e.id), "rating": e.rating, "comment": e.comment}
            for e in entries
        ]
    }
