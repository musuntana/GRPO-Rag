from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ragos.core.errors import NotFoundError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import Task
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from sqlmodel import Session, select

#: Maximum number of times a task will be retried before being permanently
#: marked ``failed``. A task's ``attempts`` counter is incremented on each
#: claim, so this cap includes the first try.
DEFAULT_MAX_ATTEMPTS = 5

#: Base exponential-backoff interval. Delay grows as
#: ``base * 2**(attempts - 1)``, capped at ``MAX_BACKOFF_SECONDS``.
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300


def compute_retry_delay(
    attempts: int,
    *,
    base_seconds: int = BASE_BACKOFF_SECONDS,
    max_seconds: int = MAX_BACKOFF_SECONDS,
) -> timedelta:
    """Exponential backoff with a hard ceiling.

    ``attempts`` is the current value from the Task row — it has already been
    incremented by ``claim_next``, so attempts=1 means "first run just failed".
    """
    if attempts < 1:
        return timedelta(seconds=base_seconds)
    seconds = min(base_seconds * (2 ** (attempts - 1)), max_seconds)
    return timedelta(seconds=seconds)


def _claim_statement(*, kind: str | None, now: datetime):
    statement = (
        select(Task)
        .where(Task.status.in_(("pending", "retryable")))
        .where((Task.available_at.is_(None)) | (Task.available_at <= now))
        .where((Task.lease_until.is_(None)) | (Task.lease_until < now))
        .order_by(Task.available_at.is_(None).desc(), Task.available_at, Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kind is not None:
        statement = statement.where(Task.kind == kind)
    return statement


def _apply_lease(session: Session, task: Task, *, lease_seconds: int, now: datetime) -> Task:
    task.status = "leased"
    task.lease_until = now + timedelta(seconds=lease_seconds)
    task.attempts += 1
    task.updated_at = now
    session.add(task)
    session.flush()
    session.refresh(task)
    return task


class TaskRepository(TenantRepository[Task]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=Task)

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: datetime | None = None,
    ) -> Task:
        existing = self.session.exec(
            self.scoped_statement().where(Task.idempotency_key == idempotency_key)
        ).first()
        if existing is not None:
            return existing

        task = Task(
            tenant_id=self.tenant_ctx.tenant_id,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            available_at=available_at,
        )
        self.session.add(task)
        self.session.flush()
        self.session.refresh(task)
        return task

    def claim_next(
        self,
        *,
        lease_seconds: int = 300,
        kind: str | None = None,
    ) -> Task | None:
        now = utc_now()
        statement = _claim_statement(kind=kind, now=now).where(
            Task.tenant_id == self.tenant_ctx.tenant_id
        )
        task = self.session.exec(statement).first()
        if task is None:
            return None
        return _apply_lease(self.session, task, lease_seconds=lease_seconds, now=now)

    def mark_completed(self, task_id: UUID) -> Task:
        task = self.require_by_id(task_id)
        task.status = "completed"
        task.lease_until = None
        task.updated_at = utc_now()
        self.session.add(task)
        self.session.flush()
        self.session.refresh(task)
        return task

    def mark_failed(
        self,
        task_id: UUID,
        *,
        retryable: bool,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Task:
        task = self.require_by_id(task_id)
        _apply_failure(task, retryable=retryable, max_attempts=max_attempts)
        self.session.add(task)
        self.session.flush()
        self.session.refresh(task)
        return task


def _apply_failure(
    task: Task,
    *,
    retryable: bool,
    max_attempts: int,
) -> None:
    """Shared failure bookkeeping: backoff, attempt cap, clear lease."""
    now = utc_now()
    task.lease_until = None
    task.updated_at = now

    if retryable and task.attempts < max_attempts:
        task.status = "retryable"
        task.available_at = now + compute_retry_delay(task.attempts)
        return

    task.status = "failed"
    # Leave available_at as-is so failed tasks stay visible in historical
    # queries ordered by available_at.


class PlatformTaskRepository:
    """Cross-tenant task repository for worker processes.

    Does NOT filter by tenant_id because workers need to pick up pending tasks
    from any tenant. Must only be used from privileged processes (workflow
    worker, scheduler worker) — never from HTTP handlers.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_next_any_tenant(
        self,
        *,
        lease_seconds: int = 300,
        kind: str | None = None,
    ) -> Task | None:
        now = utc_now()
        statement = _claim_statement(kind=kind, now=now)
        task = self.session.exec(statement).first()
        if task is None:
            return None
        return _apply_lease(self.session, task, lease_seconds=lease_seconds, now=now)

    def _get_or_raise(self, task_id: UUID) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise NotFoundError("Task not found", resource="Task", id=str(task_id))
        return task

    def mark_completed(self, task_id: UUID) -> Task:
        task = self._get_or_raise(task_id)
        task.status = "completed"
        task.lease_until = None
        task.updated_at = utc_now()
        self.session.add(task)
        self.session.flush()
        self.session.refresh(task)
        return task

    def mark_failed(
        self,
        task_id: UUID,
        *,
        retryable: bool,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Task:
        task = self._get_or_raise(task_id)
        _apply_failure(task, retryable=retryable, max_attempts=max_attempts)
        self.session.add(task)
        self.session.flush()
        self.session.refresh(task)
        return task
