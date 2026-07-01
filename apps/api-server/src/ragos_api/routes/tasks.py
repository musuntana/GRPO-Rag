from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.runtime.tasks import TaskService

from ragos_api.dependencies import DBSession, TenantCtx

router = APIRouter(
    prefix="/tenant/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_tenant_admin)],
)


class EnqueueTaskRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    available_at: datetime | None = None


class ClaimTaskRequest(BaseModel):
    lease_seconds: int = 300
    kind: str | None = None


class TaskStatusRequest(BaseModel):
    retryable: bool = False


def serialize_task(task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "tenant_id": str(task.tenant_id),
        "kind": task.kind,
        "status": task.status,
        "payload": task.payload,
        "available_at": task.available_at.isoformat() if task.available_at else None,
        "lease_until": task.lease_until.isoformat() if task.lease_until else None,
        "attempts": task.attempts,
        "idempotency_key": task.idempotency_key,
    }


@router.post("")
def enqueue_task(
    payload: EnqueueTaskRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    service = TaskService(session=session, tenant_ctx=tenant_ctx)
    task = service.enqueue(
        kind=payload.kind,
        payload=payload.payload,
        idempotency_key=payload.idempotency_key,
        available_at=payload.available_at,
    )
    return serialize_task(task)


@router.post("/claim")
def claim_task(
    payload: ClaimTaskRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any] | None:
    service = TaskService(session=session, tenant_ctx=tenant_ctx)
    task = service.claim_next(lease_seconds=payload.lease_seconds, kind=payload.kind)
    return None if task is None else serialize_task(task)


@router.post("/{task_id}/complete")
def complete_task(task_id: UUID, session: DBSession, tenant_ctx: TenantCtx) -> dict[str, Any]:
    service = TaskService(session=session, tenant_ctx=tenant_ctx)
    task = service.mark_completed(task_id)
    return serialize_task(task)


@router.post("/{task_id}/fail")
def fail_task(
    task_id: UUID,
    payload: TaskStatusRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    service = TaskService(session=session, tenant_ctx=tenant_ctx)
    task = service.mark_failed(task_id, retryable=payload.retryable)
    return serialize_task(task)
