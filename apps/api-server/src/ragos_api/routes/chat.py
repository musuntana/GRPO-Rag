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
import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
from ragos.runtime.chat_history import MessageHistoryService
from ragos.runtime.events import EventLog, EventSink, parse_last_event_id
from ragos.runtime.retrieval import (
    RetrievalService,
    build_parent_resolver,
    build_recall_channels,
)
from ragos.storage.repositories.chat_requests import ChatRequestRepository
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
    run_id: UUID,
) -> ChatService:
    """Assemble one chat turn's dependencies.

    One gateway instance serves generation, grading, query planning, the query
    embedding, extraction, and summarization, so they share a key pool and one cost
    ledger.
    """
    memory_profile = _require_memory_profile(session, tenant_ctx)
    router = ModelRouter(session)
    sink = EventSink(EventLog(), scope_key=tenant_ctx.require_private_scope_key(), run_id=run_id)

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
            persist_turn=session.commit,
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
    tenant_ctx = tenant_ctx.bind_user(user_id)

    conversation = ConversationRepository(session=session, tenant_ctx=tenant_ctx).create(
        scope_key=tenant_ctx.require_private_scope_key(),
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
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=12, max_length=128,
        pattern=r"^[A-Za-z0-9_-]{12,128}$",
    ),
):
    """One durable, actor-scoped claim is required before any model work.

    Retried keys NEVER start new model calls. Only the first SQL claimant
    receives the streaming response; follow-up requests receive the
    persisted run ID/status and may use completed-run GET for replay.
    """
    if last_event_id is not None:
        raise ValidationError(
            "Reconnection must GET the existing run events, not POST a new message"
        )
    user_id = resolve_user(session, tenant_ctx)
    tenant_ctx = tenant_ctx.bind_user(user_id)

    # Database uniqueness arbitrates concurrent requests across API workers.
    # The payload fingerprint prevents a reused key from silently binding to
    # changed instructions/model parameters. Never persist raw message text
    # in the claim record.
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    requests = ChatRequestRepository(session=session, tenant_ctx=tenant_ctx)
    request, is_new = requests.claim(
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
        request_digest=digest,
    )
    # COMMIT the claim before constructing the provider or running the model.
    session.commit()
    run_id = request.id
    if not is_new:
        response = {
            "run_id": str(run_id),
            "status": request.status,
            "events_url": (
                f"/chat/conversations/{conversation_id}/runs/{run_id}/events"
            ),
        }
        status_code = 200 if request.status == "completed" else (
            202 if request.status == "claimed" else 409
        )
        return JSONResponse(
            status_code=status_code, content=response,
            headers={"X-Run-ID": str(run_id), "Cache-Control": "no-store"},
        )

    try:
        service = _service(
            session=session, tenant_ctx=tenant_ctx, payload=payload, run_id=run_id
        )
    except Exception:
        session.rollback()
        requests.mark_failed(conversation_id=conversation_id, run_id=run_id)
        session.commit()
        raise

    def _commit_completed_turn() -> None:
        # Atomic with assistant message, citations and memory writeback task.
        requests.complete(conversation_id=conversation_id, run_id=run_id)
        session.commit()

    service.deps.persist_turn = _commit_completed_turn

    async def _produce() -> None:
        try:
            await service.run_turn(
                conversation_id=conversation_id, user_message=payload.message, run_id=run_id
            )
        except Exception:
            session.rollback()
            try:
                requests.mark_failed(conversation_id=conversation_id, run_id=run_id)
                session.commit()
            except Exception:
                session.rollback()
                # Never re-run ambiguous work after failure to acknowledge.
            await service.deps.sink.close()
            raise

    async def _stream():
        producer = asyncio.create_task(_produce())
        try:
            async for event in service.deps.sink.stream(
                last_event_id=None
            ):
                yield event.to_sse()
        finally:
            # The producer keeps writing to the run log even if the client is gone,
            # so the run stays resumable.
            await asyncio.shield(producer)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Run-ID": str(run_id),
        },
    )


# This is intentionally a finite, completed-run replay. There is no process-local
# registry or attempt to reattach a new request to an unfinished producer.
_MAX_COMPLETED_REPLAY_EVENTS = 2000
_MAX_COMPLETED_LOG_BYTES = 8 * 1024 * 1024
_MAX_REPLAY_PAGE_SIZE = 500


@router.get(
    "/conversations/{conversation_id}/runs/{run_id}/events",
    dependencies=[Depends(require_chat_actor)],
)
def replay_completed_run(
    conversation_id: UUID,
    run_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    limit: int = Query(default=200, ge=1, le=_MAX_REPLAY_PAGE_SIZE),
):
    """Return a cursor-based SSE replay without starting or billing a new turn.

    Authorization comes from a committed assistant message in the caller-owned
    conversation. No event log path can be chosen by the client; tenant/user
    scope is derived server-side and run metadata is cross-checked.
    """
    tenant_ctx = tenant_ctx.bind_user(resolve_user(session, tenant_ctx))
    messages = MessageRepository(session=session, tenant_ctx=tenant_ctx)
    message = messages.require_completed_run(conversation_id=conversation_id, run_id=run_id)
    log = EventLog()
    scope_key = tenant_ctx.require_private_scope_key()
    path = log.events_path(scope_key=scope_key, run_id=run_id)
    if not path.is_file() or path.stat().st_size > _MAX_COMPLETED_LOG_BYTES:
        raise NotFoundError("Completed chat run events not found")
    events = log.read_after(
        scope_key=scope_key,
        run_id=run_id,
        after_id=0,
        limit=_MAX_COMPLETED_REPLAY_EVENTS + 1,
    )
    if (
        not events
        or len(events) > _MAX_COMPLETED_REPLAY_EVENTS
        or events[0].event != "run_started"
        or events[0].data.get("run_id") != str(run_id)
        or events[0].data.get("conversation_id") != str(conversation_id)
        or events[-1].event != "run_finished"
        or events[-1].data.get("run_id") != str(run_id)
        or events[-1].data.get("message_id") != str(message.id)
    ):
        # A torn or mismatched event log must not be represented as a
        # successfully replayable completed run.
        raise NotFoundError("Completed chat run events not found")

    cursor = parse_last_event_id(last_event_id) or 0
    selected = [event.to_sse() for event in events if event.event_id > cursor][:limit]
    return StreamingResponse(
        iter(selected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Run-ID": str(run_id),
        },
    )


@router.get("/conversations/{conversation_id}/messages", dependencies=[Depends(require_chat_actor)])
def list_messages(
    conversation_id: UUID,
    session: DBSession,
    tenant_ctx: TenantCtx,
    limit: int = 50,
) -> dict[str, Any]:
    tenant_ctx = tenant_ctx.bind_user(resolve_user(session, tenant_ctx))
    return {
        "items": MessageHistoryService(session, tenant_ctx).list(
            conversation_id=conversation_id, limit=limit
        )
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
    tenant_ctx = tenant_ctx.bind_user(user_id)

    messages = MessageRepository(session=session, tenant_ctx=tenant_ctx)
    message = messages.require_in_conversation(message_id, conversation_id)

    entry = FeedbackRepository(session=session, tenant_ctx=tenant_ctx).record(
        scope_key=tenant_ctx.require_private_scope_key(),
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
    tenant_ctx = tenant_ctx.bind_user(resolve_user(session, tenant_ctx))
    # Require the message to belong to this conversation first: a message id from
    # another conversation is not this caller's to read feedback on.
    MessageRepository(session=session, tenant_ctx=tenant_ctx).require_in_conversation(
        message_id, conversation_id
    )
    entries = FeedbackRepository(session=session, tenant_ctx=tenant_ctx).list_for_message(
        scope_key=tenant_ctx.require_private_scope_key(), message_id=message_id
    )
    return {
        "items": [
            {"feedback_id": str(e.id), "rating": e.rating, "comment": e.comment} for e in entries
        ]
    }
