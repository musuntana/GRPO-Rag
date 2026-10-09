from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ragos.core.errors import ConflictError, NotFoundError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import Task
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import TenantRepository
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

DEFAULT_MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
ACTIVE_STATUSES = ("pending", "retryable", "leased")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timed_out")
_ERROR_CODES = frozenset(
    {
        "TASK_FAILED",
        "TASK_TRANSIENT_FAILURE",
        "TASK_ATTEMPTS_EXHAUSTED",
        "TASK_CONFIGURATION_INVALID",
        "TASK_DEADLINE_MISSING",
        "TASK_TIMEOUT",
        "TASK_CANCELLED",
        "TASK_FENCE_LOST",
        "TASK_PROCESS_FAILED",
        "TASK_PROCESS_START_FAILED",
        "TASK_WORKER_STOPPED",
        "AUTHENTICATION_REQUIRED",
        "PERMISSION_DENIED",
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "CONFLICT",
        "QUOTA_EXCEEDED",
        "PROVIDER_ERROR",
        "SANDBOX_ERROR",
        "TIMEOUT",
    }
)


def _require_duration(value: int, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TIMEOUT_SECONDS
    ):
        raise ValidationError(f"{name} must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")


def _aware(value: datetime | None) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _error_code(value: str | None, fallback: str) -> str:
    # Unknown exception text never becomes a persisted error summary.
    return value if isinstance(value, str) and value in _ERROR_CODES else fallback


def compute_retry_delay(
    attempts: int,
    *,
    base_seconds: int = BASE_BACKOFF_SECONDS,
    max_seconds: int = MAX_BACKOFF_SECONDS,
) -> timedelta:
    if attempts < 1:
        return timedelta(seconds=base_seconds)
    # Bound the exponent too; malformed counters cannot cause huge allocation.
    seconds = min(base_seconds * (2 ** min(attempts - 1, 16)), max_seconds)
    return timedelta(seconds=seconds)


def _eligible_kinds(statement, *, kind, kinds, tenant_id=None):
    if kind is not None:
        statement = statement.where(Task.kind == kind)
    if kinds is not None:
        statement = statement.where(Task.kind.in_(kinds))
    if tenant_id is not None:
        statement = statement.where(Task.tenant_id == tenant_id)
    return statement


def _claim_statement(*, kind: str | None, now: datetime, kinds: tuple[str, ...] | None = None):
    expired = or_(Task.lease_until.is_(None), Task.lease_until <= now)
    statement = (
        select(Task)
        .where(Task.attempts < DEFAULT_MAX_ATTEMPTS)
        .where(Task.timeout_seconds.between(1, MAX_TIMEOUT_SECONDS))
        .where(Task.cancel_requested_at.is_(None))
        .where(or_(Task.deadline_at.is_(None), Task.deadline_at > now))
        .where(or_(Task.attempts == 0, Task.deadline_at.is_not(None)))
        .where(
            or_(Task.status.in_(("pending", "retryable")), and_(Task.status == "leased", expired))
        )
        .where(or_(Task.available_at.is_(None), Task.available_at <= now))
        .where(expired)
        .order_by(Task.available_at.is_(None).desc(), Task.available_at, Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return _eligible_kinds(statement, kind=kind, kinds=kinds)


def _finish(task: Task, *, status: str, now: datetime, error_code: str | None) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValidationError("Unknown task terminal state")
    task.status = status
    task.lease_until = None
    task.finished_at = now
    task.updated_at = now
    task.error_code = None if status == "completed" else _error_code(error_code, "TASK_FAILED")


def _sweep_unexecutable(
    session: Session, *, now: datetime, kind=None, kinds=None, tenant_id=None
) -> None:
    # A live leased process is terminated by its parent before terminal ack.
    # An expired orphan is logically fenced; this is not proof of remote kill.
    safe_to_finish = or_(
        Task.status.in_(("pending", "retryable")),
        and_(Task.status == "leased", or_(Task.lease_until.is_(None), Task.lease_until <= now)),
    )
    reason = or_(
        Task.cancel_requested_at.is_not(None),
        Task.deadline_at <= now,
        Task.attempts >= DEFAULT_MAX_ATTEMPTS,
        and_(Task.attempts > 0, Task.deadline_at.is_(None)),
        ~Task.timeout_seconds.between(1, MAX_TIMEOUT_SECONDS),
    )
    statement = _eligible_kinds(
        select(Task)
        .where(safe_to_finish, reason)
        .order_by(Task.created_at)
        .limit(100)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True),
        kind=kind,
        kinds=kinds,
        tenant_id=tenant_id,
    )
    for task in session.exec(statement).all():
        if (
            task.status not in ACTIVE_STATUSES
            or (tenant_id is not None and task.tenant_id != tenant_id)
            or (kind is not None and task.kind != kind)
            or (kinds is not None and task.kind not in kinds)
            or (task.status == "leased" and _aware(task.lease_until) and task.lease_until > now)
        ):
            continue
        if task.deadline_at is not None and task.deadline_at <= now:
            _finish(task, status="timed_out", now=now, error_code="TASK_TIMEOUT")
        elif task.cancel_requested_at is not None:
            _finish(task, status="cancelled", now=now, error_code="TASK_CANCELLED")
        elif task.attempts > 0 and task.deadline_at is None:
            _finish(task, status="failed", now=now, error_code="TASK_DEADLINE_MISSING")
        elif task.attempts >= DEFAULT_MAX_ATTEMPTS:
            _finish(task, status="failed", now=now, error_code="TASK_ATTEMPTS_EXHAUSTED")
        elif (
            isinstance(task.timeout_seconds, bool)
            or not isinstance(task.timeout_seconds, int)
            or not 1 <= task.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            _finish(task, status="failed", now=now, error_code="TASK_CONFIGURATION_INVALID")
        else:
            continue
        session.add(task)
    session.flush()


def _apply_lease(session: Session, task: Task, *, lease_seconds: int, now: datetime) -> Task:
    _require_duration(lease_seconds, "lease_seconds")
    _require_duration(task.timeout_seconds, "timeout_seconds")
    if (
        task.status not in ACTIVE_STATUSES
        or isinstance(task.attempts, bool)
        or not isinstance(task.attempts, int)
        or task.attempts < 0
        or task.cancel_requested_at is not None
        or task.attempts >= DEFAULT_MAX_ATTEMPTS
        or (task.status == "leased" and _aware(task.lease_until) and task.lease_until > now)
    ):
        raise ConflictError("Task is not executable")
    if task.deadline_at is None:
        if task.attempts != 0:
            raise ConflictError("Original task deadline is unavailable")
        task.deadline_at = now + timedelta(seconds=task.timeout_seconds)
    if not _aware(task.deadline_at) or task.deadline_at <= now:
        raise ConflictError("Task deadline expired")
    task.status = "leased"
    task.lease_until = min(now + timedelta(seconds=lease_seconds), task.deadline_at)
    task.attempts += 1
    task.finished_at = None
    task.error_code = None
    task.updated_at = now
    session.add(task)
    session.flush()
    session.refresh(task)
    return task


def _require_generation(task: Task, *, expected_attempts: int) -> None:
    if (
        isinstance(expected_attempts, bool)
        or not isinstance(expected_attempts, int)
        or expected_attempts < 1
        or task.status != "leased"
        or task.attempts != expected_attempts
    ):
        raise ConflictError("Task lease has been superseded", task_id=str(task.id))


def _require_claim(task: Task, *, expected_attempts: int, now: datetime | None = None) -> None:
    _require_generation(task, expected_attempts=expected_attempts)
    current = now or utc_now()
    if not _aware(task.lease_until) or task.lease_until <= current:
        raise ConflictError("Task lease expired", task_id=str(task.id))
    if not _aware(task.deadline_at) or task.deadline_at <= current:
        raise ConflictError("Task deadline expired", task_id=str(task.id))
    if task.cancel_requested_at is not None:
        raise ConflictError("Task cancellation requested", task_id=str(task.id))


def _lock_task(session: Session, task_id: UUID, tenant_id: UUID) -> Task:
    if not isinstance(tenant_id, UUID) or not isinstance(task_id, UUID):
        raise ValidationError("Task and tenant identities must be UUIDs")
    task = session.exec(
        select(Task)
        .where(Task.id == task_id, Task.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if task is None:
        raise NotFoundError("Task not found", resource="Task", id=str(task_id))
    # SQL scope is essential even on a privileged connection; assert the result
    # too so adapters cannot accidentally widen the operation.
    if task.tenant_id != tenant_id:
        raise NotFoundError("Task not found", resource="Task", id=str(task_id))
    return task


def _save(session: Session, task: Task) -> Task:
    session.add(task)
    session.flush()
    session.refresh(task)
    return task


def _complete(session, task_id, tenant_id, expected_attempts):
    task = _lock_task(session, task_id, tenant_id)
    now = utc_now()
    _require_claim(task, expected_attempts=expected_attempts, now=now)
    _finish(task, status="completed", now=now, error_code=None)
    return _save(session, task)


def _apply_failure(
    task: Task,
    *,
    retryable: bool,
    max_attempts: int,
    error_code: str | None = None,
    now: datetime | None = None,
) -> None:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= DEFAULT_MAX_ATTEMPTS
    ):
        raise ValidationError("Retry attempt cap must stay within the worker budget")
    current = now or utc_now()
    _require_claim(task, expected_attempts=task.attempts, now=current)
    retry_at = current + compute_retry_delay(task.attempts)
    if retryable and task.attempts < max_attempts and retry_at < task.deadline_at:
        task.status = "retryable"
        task.lease_until = None
        task.available_at = retry_at
        task.finished_at = None
        task.updated_at = current
        task.error_code = _error_code(error_code, "TASK_TRANSIENT_FAILURE")
        return
    # An expired deadline is acked through mark_timed_out, not an ordinary
    # failed-lease call. Insufficient time for the next retry ends as failed.
    final_code = (
        "TASK_ATTEMPTS_EXHAUSTED" if retryable and task.attempts >= max_attempts else "TASK_FAILED"
    )
    _finish(task, status="failed", now=current, error_code=_error_code(error_code, final_code))


def _fail(session, task_id, tenant_id, expected_attempts, retryable, max_attempts, error_code):
    task = _lock_task(session, task_id, tenant_id)
    _require_claim(task, expected_attempts=expected_attempts)
    _apply_failure(task, retryable=retryable, max_attempts=max_attempts, error_code=error_code)
    return _save(session, task)


def _request_cancel(session, task_id, tenant_id):
    task = _lock_task(session, task_id, tenant_id)
    if task.status in TERMINAL_STATUSES:
        return task
    now = utc_now()
    if task.cancel_requested_at is None:
        task.cancel_requested_at = now
    if task.status in {"pending", "retryable"}:
        if task.deadline_at is not None and task.deadline_at <= now:
            _finish(task, status="timed_out", now=now, error_code="TASK_TIMEOUT")
        else:
            _finish(task, status="cancelled", now=now, error_code="TASK_CANCELLED")
    elif task.status == "leased":
        task.updated_at = now
    else:
        raise ConflictError("Task state is not cancellable")
    return _save(session, task)


def _renew(session, task_id, tenant_id, expected_attempts, lease_seconds):
    _require_duration(lease_seconds, "lease_seconds")
    task = _lock_task(session, task_id, tenant_id)
    now = utc_now()
    _require_claim(task, expected_attempts=expected_attempts, now=now)
    task.lease_until = min(
        max(task.lease_until, now + timedelta(seconds=lease_seconds)), task.deadline_at
    )
    task.updated_at = now
    return _save(session, task)


def _ack_stop(session, task_id, tenant_id, expected_attempts, *, cancelled):
    task = _lock_task(session, task_id, tenant_id)
    _require_generation(task, expected_attempts=expected_attempts)
    now = utc_now()
    if cancelled:
        if task.cancel_requested_at is None:
            raise ConflictError("Cancellation has not been requested")
        _finish(task, status="cancelled", now=now, error_code="TASK_CANCELLED")
    else:
        if not _aware(task.deadline_at) or task.deadline_at > now:
            raise ConflictError("Task deadline has not expired")
        _finish(task, status="timed_out", now=now, error_code="TASK_TIMEOUT")
    return _save(session, task)


def _claim_selected(session: Session, task: Task | None, *, lease_seconds: int) -> Task | None:
    if task is None:
        return None
    current = utc_now()
    # The query may have run just before the deadline. Its row lock is now
    # held, so finish an elapsed candidate instead of aborting the dispatch.
    if _aware(task.deadline_at) and task.deadline_at <= current:
        _finish(task, status="timed_out", now=current, error_code="TASK_TIMEOUT")
        _save(session, task)
        return None
    return _apply_lease(session, task, lease_seconds=lease_seconds, now=current)


class TaskRepository(TenantRepository[Task]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_scope_key()
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=Task)

    def lock_preparation_key(self, key: str) -> None:
        self.tenant_ctx.require_scope_key()
        namespace = f"{self.tenant_ctx.require_scope_key()}:prepare:{key}"
        self.session.exec(select(func.pg_advisory_xact_lock(func.hashtextextended(namespace, 0))))

    def get_by_idempotency_key(self, key: str) -> Task | None:
        return self.session.exec(self.scoped_statement().where(Task.idempotency_key == key)).first()

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: datetime | None = None,
        timeout_seconds: int = 300,
    ) -> Task:
        _require_duration(timeout_seconds, "timeout_seconds")
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 64:
            raise ValidationError("Task kind is required")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 255
        ):
            raise ValidationError("Task idempotency key is required")
        if available_at is not None and not _aware(available_at):
            raise ValidationError("Task availability requires a timezone")
        statement = self.scoped_statement().where(Task.idempotency_key == idempotency_key)

        def existing_task():
            existing = self.session.exec(statement).first()
            if existing is not None and (
                existing.kind != kind
                or existing.payload != payload
                or existing.timeout_seconds != timeout_seconds
            ):
                raise ConflictError("Idempotency key reused with different task input")
            return existing

        existing = existing_task()
        if existing is not None:
            return existing
        task = Task(
            tenant_id=self.tenant_ctx.tenant_id,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            available_at=available_at,
            timeout_seconds=timeout_seconds,
        )
        try:
            with self.session.begin_nested():
                self.session.add(task)
                self.session.flush()
        except IntegrityError:
            existing = existing_task()
            if existing is None:
                raise
            return existing
        self.session.refresh(task)
        return task

    def claim_next(self, *, lease_seconds: int = 300, kind: str | None = None) -> Task | None:
        _require_duration(lease_seconds, "lease_seconds")
        now = utc_now()
        _sweep_unexecutable(self.session, now=now, kind=kind, tenant_id=self.tenant_ctx.tenant_id)
        statement = _claim_statement(kind=kind, now=now).where(
            Task.tenant_id == self.tenant_ctx.tenant_id
        )
        task = self.session.exec(statement).first()
        return _claim_selected(self.session, task, lease_seconds=lease_seconds)

    def lock_claim(self, task_id: UUID, *, expected_attempts: int) -> Task:
        task = _lock_task(self.session, task_id, self.tenant_ctx.tenant_id)
        _require_claim(task, expected_attempts=expected_attempts)
        return task

    def mark_completed(self, task_id: UUID, *, expected_attempts: int) -> Task:
        return _complete(self.session, task_id, self.tenant_ctx.tenant_id, expected_attempts)

    def mark_failed(
        self,
        task_id: UUID,
        *,
        retryable: bool,
        expected_attempts: int,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        error_code: str | None = None,
    ) -> Task:
        return _fail(
            self.session,
            task_id,
            self.tenant_ctx.tenant_id,
            expected_attempts,
            retryable,
            max_attempts,
            error_code,
        )

    def request_cancel(self, task_id: UUID) -> Task:
        return _request_cancel(self.session, task_id, self.tenant_ctx.tenant_id)

    def renew_lease(
        self, task_id: UUID, *, expected_attempts: int, lease_seconds: int = 300
    ) -> Task:
        return _renew(
            self.session, task_id, self.tenant_ctx.tenant_id, expected_attempts, lease_seconds
        )

    def mark_cancelled(self, task_id: UUID, *, expected_attempts: int) -> Task:
        return _ack_stop(
            self.session, task_id, self.tenant_ctx.tenant_id, expected_attempts, cancelled=True
        )

    def mark_timed_out(self, task_id: UUID, *, expected_attempts: int) -> Task:
        return _ack_stop(
            self.session, task_id, self.tenant_ctx.tenant_id, expected_attempts, cancelled=False
        )


class PlatformTaskRepository:
    """Explicit privileged claim entry; every task mutation still names its tenant."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_next_any_tenant(
        self, *, lease_seconds: int = 300, kind: str | None = None, kinds: tuple[str, ...]
    ) -> Task | None:
        _require_duration(lease_seconds, "lease_seconds")
        if kind is not None and kind not in kinds:
            raise ValidationError("Task kind is not registered for this worker")
        if not kinds:
            return None
        now = utc_now()
        _sweep_unexecutable(self.session, now=now, kind=kind, kinds=kinds)
        task = self.session.exec(_claim_statement(kind=kind, now=now, kinds=kinds)).first()
        return _claim_selected(self.session, task, lease_seconds=lease_seconds)

    def lock_claim(
        self, task_id: UUID, *, expected_attempts: int, expected_tenant_id: UUID
    ) -> Task:
        task = _lock_task(self.session, task_id, expected_tenant_id)
        _require_claim(task, expected_attempts=expected_attempts)
        return task

    def mark_completed(
        self, task_id: UUID, *, expected_attempts: int, expected_tenant_id: UUID
    ) -> Task:
        return _complete(self.session, task_id, expected_tenant_id, expected_attempts)

    def mark_failed(
        self,
        task_id: UUID,
        *,
        retryable: bool,
        expected_attempts: int,
        expected_tenant_id: UUID,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        error_code: str | None = None,
    ) -> Task:
        return _fail(
            self.session,
            task_id,
            expected_tenant_id,
            expected_attempts,
            retryable,
            max_attempts,
            error_code,
        )

    def request_cancel(self, task_id: UUID, *, expected_tenant_id: UUID) -> Task:
        return _request_cancel(self.session, task_id, expected_tenant_id)

    def renew_lease(
        self,
        task_id: UUID,
        *,
        expected_attempts: int,
        expected_tenant_id: UUID,
        lease_seconds: int = 300,
    ) -> Task:
        return _renew(self.session, task_id, expected_tenant_id, expected_attempts, lease_seconds)

    def mark_cancelled(
        self, task_id: UUID, *, expected_attempts: int, expected_tenant_id: UUID
    ) -> Task:
        return _ack_stop(
            self.session, task_id, expected_tenant_id, expected_attempts, cancelled=True
        )

    def mark_timed_out(
        self, task_id: UUID, *, expected_attempts: int, expected_tenant_id: UUID
    ) -> Task:
        return _ack_stop(
            self.session, task_id, expected_tenant_id, expected_attempts, cancelled=False
        )
