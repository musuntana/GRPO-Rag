from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ragos.core.errors import ConflictError, NotFoundError, ValidationError
from ragos.core.errors import TimeoutError as DomainTimeoutError
from ragos.core.logging import get_logger
from ragos.core.tenant import ActorType, TenantContext
from ragos.runtime.process_runner import ProcessRunner, TaskProcessResult
from ragos.storage.db import bind_session_tenant
from ragos.storage.models.app import Task, Tenant
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.tasks import PlatformTaskRepository, TaskRepository
from sqlmodel import Session

LOGGER = get_logger(__name__)

#: Values accepted by ``TenantContext.security_level``. Kept local to avoid an
#: import cycle with the Pydantic literal definition.
_VALID_SECURITY_LEVELS = frozenset({"public", "internal", "restricted"})


class TaskService:
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx
        self.repo = TaskRepository(session=session, tenant_ctx=tenant_ctx)

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        available_at: datetime | None = None,
        timeout_seconds: int = 300,
    ) -> Task:
        with self.session.begin():
            task = self.repo.enqueue(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=available_at,
                timeout_seconds=timeout_seconds,
            )
        return task

    def claim_next(self, *, lease_seconds: int = 300, kind: str | None = None) -> Task | None:
        with self.session.begin():
            return self.repo.claim_next(lease_seconds=lease_seconds, kind=kind)

    def mark_completed(self, task_id: UUID, *, expected_attempts: int) -> Task:
        with self.session.begin():
            return self.repo.mark_completed(task_id, expected_attempts=expected_attempts)

    def mark_failed(self, task_id: UUID, *, retryable: bool, expected_attempts: int) -> Task:
        with self.session.begin():
            return self.repo.mark_failed(
                task_id, retryable=retryable, expected_attempts=expected_attempts
            )

    def request_cancel(self, task_id: UUID) -> Task:
        with self.session.begin():
            return self.repo.request_cancel(task_id)


# Worker-side task dispatch: short leases, immutable envelope, isolated execution.


class ClaimedTaskRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    tenant_id: UUID
    kind: str = Field(min_length=1, max_length=64)
    attempts: int = Field(ge=1, strict=True)
    lease_until: datetime
    deadline_at: datetime

    @field_validator("lease_until", "deadline_at")
    @classmethod
    def require_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task execution timestamps must be timezone-aware")
        return value


class TaskEnvelope(ClaimedTaskRef):
    payload_json: str = Field(max_length=65536)

    @property
    def payload(self) -> dict:
        # Fresh decode; a handler cannot change this envelope by mutating a nested dictionary.
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class TaskCommand:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    replay_safe: bool = False

    def __post_init__(self):
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


class TaskCommandBuilder(Protocol):
    def __call__(self, *, task: TaskEnvelope, tenant_ctx: TenantContext) -> TaskCommand: ...


class TaskHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, TaskCommandBuilder] = {}

    def register(self, kind: str, handler: TaskCommandBuilder) -> None:
        if not kind or kind in self._handlers:
            raise ValidationError("Task kind must have one server-registered command builder")
        self._handlers[kind] = handler

    def resolve(self, kind: str) -> TaskCommandBuilder | None:
        return self._handlers.get(kind)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._handlers)


def build_worker_tenant_ctx(
    tenant_id: UUID, session: Session, *, trace_id: str, actor_id: str = "task-worker"
) -> TenantContext:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    if not tenant.is_active:
        raise ValidationError("Inactive tenant cannot execute tasks")
    if tenant.security_level not in _VALID_SECURITY_LEVELS:
        raise ValidationError("Tenant security level is invalid")
    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug=tenant.slug,
        actor_id=actor_id,
        actor_type=ActorType.SERVICE,
        roles=["service"],
        plan_tier=tenant.plan_tier,
        locale=tenant.locale,
        timezone=tenant.timezone,
        security_level=tenant.security_level,
        trace_id=trace_id,
    )


def _claim_one(
    session_factory, *, lease_seconds: int, kind: str | None, kinds: tuple[str, ...]
) -> ClaimedTaskRef | None:
    with session_factory() as session, session.begin():
        task = PlatformTaskRepository(session).claim_next_any_tenant(
            lease_seconds=lease_seconds, kind=kind, kinds=kinds
        )
        if task is None:
            return None
        return ClaimedTaskRef(
            id=task.id,
            tenant_id=task.tenant_id,
            kind=task.kind,
            attempts=task.attempts,
            lease_until=task.lease_until,
            deadline_at=task.deadline_at,
        )


def _scoped_transaction(session_factory, tenant_id):
    from contextlib import contextmanager

    @contextmanager
    def transaction():
        with session_factory() as session:
            bind_session_tenant(session, tenant_id)
            with session.begin():
                yield session

    return transaction()


def _execute_claimed(
    session_factory,
    ref: ClaimedTaskRef,
    registry: TaskHandlerRegistry,
    *,
    runner: ProcessRunner | None = None,
    lease_seconds: int = 300,
    shutdown: Callable[[], bool] | None = None,
) -> None:
    handler = registry.resolve(ref.kind)
    try:
        with _scoped_transaction(session_factory, ref.tenant_id) as session:
            repo = PlatformTaskRepository(session)
            row = repo.lock_claim(
                ref.id, expected_attempts=ref.attempts, expected_tenant_id=ref.tenant_id
            )
            if row.kind != ref.kind:
                raise ConflictError("Claimed task kind changed")
            tenant_ctx = build_worker_tenant_ctx(
                ref.tenant_id, session, trace_id=f"worker-{ref.id}"
            )
            from ragos.storage.artifacts import canonical_json

            envelope = TaskEnvelope(
                **ref.model_dump(), payload_json=canonical_json(row.payload).decode("utf-8")
            )

    except Exception as exc:
        LOGGER.warning(
            "task preparation failed",
            extra={"task_id": str(ref.id), "error_type": type(exc).__name__},
        )
        _record_terminal(session_factory, ref, TaskProcessResult(None, "TASK_FAILED"))
        return

    # All business execution is outside the preparation transaction and row lock.
    def cancellation():
        with _scoped_transaction(session_factory, ref.tenant_id) as session:
            row = session.get(Task, ref.id)
            if row is None or row.tenant_id != ref.tenant_id or row.attempts != ref.attempts:
                raise ConflictError("Task execution fence changed")
            return row.cancel_requested_at is not None

    def heartbeat():
        with _scoped_transaction(session_factory, ref.tenant_id) as session:
            PlatformTaskRepository(session).renew_lease(
                ref.id,
                expected_attempts=ref.attempts,
                expected_tenant_id=ref.tenant_id,
                lease_seconds=lease_seconds,
            )

    result = TaskProcessResult(None, "TASK_PROCESS_FAILED")
    replay_safe = False
    try:
        if handler is None:
            raise ValidationError("Task has no registered process")
        command = handler(task=envelope, tenant_ctx=tenant_ctx)
        replay_safe = command.replay_safe
        remaining = (envelope.deadline_at - utc_now()).total_seconds()
        if remaining <= 0:
            result = TaskProcessResult(None, "TASK_TIMEOUT")
        else:
            result = (runner or ProcessRunner()).run(
                command.argv,
                env=command.env,
                deadline_seconds=remaining,
                cancellation=cancellation,
                heartbeat=heartbeat,
                heartbeat_interval=max(0.1, min(1.0, lease_seconds / 3)),
                shutdown=shutdown,
            )
    except (TimeoutError, DomainTimeoutError):
        result = TaskProcessResult(None, "TASK_TIMEOUT")
    except Exception as exc:
        LOGGER.warning(
            "task command failed",
            extra={"task_id": str(ref.id), "kind": ref.kind, "error_type": type(exc).__name__},
        )
    _record_terminal(
        session_factory,
        ref,
        result,
        retryable=(result.error_code == "TASK_WORKER_STOPPED" and replay_safe),
    )


def _record_terminal(session_factory, ref, result, *, retryable=False):
    with _scoped_transaction(session_factory, ref.tenant_id) as session:
        repo = PlatformTaskRepository(session)
        row = session.get(Task, ref.id)
        if row is None or row.tenant_id != ref.tenant_id or row.attempts != ref.attempts:
            return
        if row.status != "leased":
            return
        try:
            if row.cancel_requested_at is not None:
                repo.mark_cancelled(
                    ref.id, expected_attempts=ref.attempts, expected_tenant_id=ref.tenant_id
                )
            elif row.deadline_at is not None and row.deadline_at <= utc_now():
                repo.mark_timed_out(
                    ref.id, expected_attempts=ref.attempts, expected_tenant_id=ref.tenant_id
                )
            elif result.exit_code == 0 and result.error_code is None:
                repo.mark_completed(
                    ref.id, expected_attempts=ref.attempts, expected_tenant_id=ref.tenant_id
                )
            else:
                repo.mark_failed(
                    ref.id,
                    expected_attempts=ref.attempts,
                    expected_tenant_id=ref.tenant_id,
                    retryable=retryable,
                    error_code=result.error_code,
                )
        except ConflictError:
            LOGGER.info("terminal from stale execution ignored", extra={"task_id": str(ref.id)})


def process_one_task(
    session_factory,
    registry: TaskHandlerRegistry,
    *,
    lease_seconds: int = 300,
    kind: str | None = None,
    runner: ProcessRunner | None = None,
    shutdown: Callable[[], bool] | None = None,
) -> bool:
    if isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValidationError("Lease duration must be positive")
    if kind is not None and registry.resolve(kind) is None:
        raise ValidationError("Worker task kind is not registered")
    if not registry.kinds:
        return False
    ref = _claim_one(session_factory, lease_seconds=lease_seconds, kind=kind, kinds=registry.kinds)
    if ref is None:
        return False
    _execute_claimed(
        session_factory,
        ref,
        registry,
        runner=runner,
        lease_seconds=lease_seconds,
        shutdown=shutdown,
    )
    return True
