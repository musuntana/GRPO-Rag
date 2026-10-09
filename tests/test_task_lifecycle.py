"""Deterministic lifecycle boundaries; mock adapters are not PostgreSQL proof."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError, NotFoundError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import Task
from ragos.storage.repositories import tasks as module
from ragos.storage.repositories.tasks import (
    DEFAULT_MAX_ATTEMPTS,
    PlatformTaskRepository,
    TaskRepository,
    _apply_failure,
    _apply_lease,
    _claim_statement,
    _require_claim,
)
from sqlalchemy.dialects import postgresql

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _ctx():
    return TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="service", trace_id="test")


def _task(ctx=None, **overrides):
    ctx = ctx or _ctx()
    fields = dict(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        kind="known",
        payload={},
        idempotency_key=f"request-{uuid4()}",
        status="leased",
        attempts=1,
        timeout_seconds=300,
        lease_until=NOW + timedelta(seconds=30),
        deadline_at=NOW + timedelta(seconds=300),
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return Task(**fields)


class StoredTaskSession:
    """Captures queries and mutates an ORM object without a live database."""

    def __init__(self, task=None, rows=()):
        self.task = task
        self.rows = list(rows)
        self.statements = []
        self.writes = []

    def exec(self, statement):
        self.statements.append(statement)
        return self

    def first(self):
        return self.task

    def all(self):
        return self.rows

    def add(self, task):
        self.writes.append(task)

    def flush(self):
        pass

    def refresh(self, task):
        pass


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(module, "utc_now", lambda: NOW)


@pytest.mark.parametrize("value", [0, -1, 3601, True, 1.5, "300"])
def test_invalid_timeout_refused_before_enqueue_lookup(value):
    session = StoredTaskSession()
    with pytest.raises(ValidationError):
        TaskRepository(session, _ctx()).enqueue(
            kind="known", payload={}, idempotency_key="same", timeout_seconds=value
        )
    assert not session.statements


def test_idempotency_does_not_allow_timeout_budget_rewrite():
    ctx = _ctx()
    task = _task(ctx, timeout_seconds=30)
    repo = TaskRepository(StoredTaskSession(task), ctx)
    assert (
        repo.enqueue(
            kind="known", payload={}, idempotency_key=task.idempotency_key, timeout_seconds=30
        )
        is task
    )
    with pytest.raises(ConflictError):
        repo.enqueue(
            kind="known", payload={}, idempotency_key=task.idempotency_key, timeout_seconds=300
        )
    assert task.timeout_seconds == 30


def test_first_claim_and_reclaim_share_one_absolute_deadline():
    task = _task(
        status="pending", attempts=0, deadline_at=None, lease_until=None, timeout_seconds=10
    )
    session = StoredTaskSession(task)
    _apply_lease(session, task, lease_seconds=3, now=NOW)
    first_deadline = task.deadline_at
    assert first_deadline == NOW + timedelta(seconds=10)
    assert task.attempts == 1
    _apply_lease(session, task, lease_seconds=30, now=NOW + timedelta(seconds=4))
    assert task.deadline_at == first_deadline
    assert task.lease_until == first_deadline
    assert task.attempts == 2
    with pytest.raises(ConflictError):
        _require_claim(task, expected_attempts=1, now=NOW + timedelta(seconds=4))


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_until": NOW},
        {"lease_until": NOW - timedelta(seconds=1)},
        {"lease_until": None},
        {"deadline_at": NOW},
        {"deadline_at": None},
        {"cancel_requested_at": NOW},
        {"attempts": 2},
        {"status": "completed"},
    ],
)
def test_completion_rejects_invalid_generation_even_without_reclaim(changes):
    ctx = _ctx()
    task = _task(ctx, **changes)
    session = StoredTaskSession(task)
    with pytest.raises(ConflictError):
        TaskRepository(session, ctx).mark_completed(task.id, expected_attempts=1)
    assert not session.writes


def test_valid_completion_records_terminal_summary_without_error_text():
    ctx = _ctx()
    task = _task(ctx)
    TaskRepository(StoredTaskSession(task), ctx).mark_completed(task.id, expected_attempts=1)
    assert task.status == "completed"
    assert task.finished_at == NOW
    assert task.lease_until is None
    assert task.error_code is None


@pytest.mark.parametrize("status", ["pending", "retryable"])
def test_cancel_waiting_task_is_immediate_and_idempotent(status):
    ctx = _ctx()
    task = _task(ctx, status=status, lease_until=None)
    repo = TaskRepository(StoredTaskSession(task), ctx)
    repo.request_cancel(task.id)
    assert task.status == "cancelled"
    assert task.cancel_requested_at == NOW
    assert task.finished_at == NOW
    assert task.error_code == "TASK_CANCELLED"
    finished = task.finished_at
    assert repo.request_cancel(task.id) is task
    assert task.finished_at == finished


def test_live_cancel_is_intent_until_parent_acknowledges_stop():
    ctx = _ctx()
    task = _task(ctx)
    repo = TaskRepository(StoredTaskSession(task), ctx)
    repo.request_cancel(task.id)
    assert task.status == "leased"
    assert task.finished_at is None
    with pytest.raises(ConflictError):
        repo.mark_completed(task.id, expected_attempts=1)
    with pytest.raises(ConflictError):
        repo.renew_lease(task.id, expected_attempts=1)
    repo.mark_cancelled(task.id, expected_attempts=1)
    assert task.status == "cancelled"
    assert task.finished_at == NOW


@pytest.mark.parametrize(
    "method,changes",
    [
        ("mark_cancelled", {}),
        ("mark_timed_out", {}),
        ("mark_cancelled", {"cancel_requested_at": NOW, "attempts": 2}),
        ("mark_timed_out", {"deadline_at": NOW, "attempts": 2}),
    ],
)
def test_stop_ack_requires_corresponding_intent_or_deadline_and_current_attempt(method, changes):
    ctx = _ctx()
    task = _task(ctx, **changes)
    with pytest.raises(ConflictError):
        getattr(TaskRepository(StoredTaskSession(task), ctx), method)(task.id, expected_attempts=1)
    assert task.status == "leased"


def test_deadline_ack_can_finalize_its_generation_after_lease_expiry():
    ctx = _ctx()
    task = _task(ctx, deadline_at=NOW, lease_until=NOW)
    TaskRepository(StoredTaskSession(task), ctx).mark_timed_out(task.id, expected_attempts=1)
    assert task.status == "timed_out"
    assert task.finished_at == NOW
    assert task.error_code == "TASK_TIMEOUT"


def test_renewal_is_monotonic_and_capped_at_original_deadline():
    ctx = _ctx()
    task = _task(ctx, deadline_at=NOW + timedelta(seconds=100))
    repo = TaskRepository(StoredTaskSession(task), ctx)
    repo.renew_lease(task.id, expected_attempts=1, lease_seconds=5)
    assert task.lease_until == NOW + timedelta(seconds=30)
    repo.renew_lease(task.id, expected_attempts=1, lease_seconds=300)
    assert task.lease_until == task.deadline_at == NOW + timedelta(seconds=100)
    assert task.attempts == 1


def test_expired_lease_cannot_be_revived_by_heartbeat():
    ctx = _ctx()
    task = _task(ctx, lease_until=NOW)
    with pytest.raises(ConflictError):
        TaskRepository(StoredTaskSession(task), ctx).renew_lease(task.id, expected_attempts=1)
    assert task.lease_until == NOW


def test_transient_retry_preserves_deadline_and_respects_remaining_budget():
    task = _task(deadline_at=NOW + timedelta(seconds=100))
    deadline = task.deadline_at
    _apply_failure(task, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS, now=NOW)
    assert task.status == "retryable"
    assert task.deadline_at == deadline
    assert task.finished_at is None
    exhausted = _task(deadline_at=NOW + timedelta(seconds=4))
    _apply_failure(exhausted, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS, now=NOW)
    assert exhausted.status == "failed"
    assert exhausted.finished_at == NOW
    assert exhausted.deadline_at == NOW + timedelta(seconds=4)


def test_raw_exception_cannot_be_persisted_in_error_code():
    task = _task()
    _apply_failure(
        task,
        retryable=False,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        error_code="api_key=private-secret /internal/path",
        now=NOW,
    )
    assert task.error_code == "TASK_FAILED"


@pytest.mark.parametrize(
    "status,changes,expected,code",
    [
        ("pending", {"deadline_at": NOW}, "timed_out", "TASK_TIMEOUT"),
        ("retryable", {"cancel_requested_at": NOW}, "cancelled", "TASK_CANCELLED"),
        (
            "leased",
            {"lease_until": NOW, "attempts": DEFAULT_MAX_ATTEMPTS},
            "failed",
            "TASK_ATTEMPTS_EXHAUSTED",
        ),
        ("leased", {"lease_until": NOW, "deadline_at": None}, "failed", "TASK_DEADLINE_MISSING"),
        ("leased", {"lease_until": NOW, "deadline_at": NOW}, "timed_out", "TASK_TIMEOUT"),
    ],
)
def test_sweep_persists_deterministic_terminal_summaries(status, changes, expected, code):
    ctx = _ctx()
    task = _task(ctx, status=status, **changes)
    session = StoredTaskSession(rows=[task])
    module._sweep_unexecutable(session, now=NOW, kind="known", tenant_id=ctx.tenant_id)
    assert task.status == expected
    assert task.error_code == code
    assert task.finished_at == NOW
    assert task.lease_until is None
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "app.task.kind = 'known'" in sql
    assert f"app.task.tenant_id = '{ctx.tenant_id}'" in sql
    assert "SKIP LOCKED" in sql


def test_claim_query_excludes_cancelled_expired_and_unregistered_work():
    stmt = _claim_statement(kind=None, now=NOW, kinds=("known",))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "app.task.cancel_requested_at IS NULL" in sql
    assert "app.task.deadline_at >" in sql
    assert "app.task.kind IN ('known')" in sql
    assert "app.task.lease_until <=" in sql
    assert "SKIP LOCKED" in sql


def test_sweep_query_does_not_acknowledge_a_still_live_cancelled_process():
    session = StoredTaskSession()
    module._sweep_unexecutable(session, now=NOW, kinds=("known",))
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "app.task.status = 'leased'" in sql
    assert "app.task.lease_until <=" in sql
    assert "app.task.kind IN ('known')" in sql


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("mark_completed", {"expected_attempts": 1}),
        ("mark_failed", {"expected_attempts": 1, "retryable": False}),
        ("request_cancel", {}),
        ("renew_lease", {"expected_attempts": 1}),
        ("mark_cancelled", {"expected_attempts": 1}),
        ("mark_timed_out", {"expected_attempts": 1}),
    ],
)
def test_privileged_connection_still_requires_exact_tenant(method, kwargs):
    task = _task(cancel_requested_at=NOW, deadline_at=NOW)
    session = StoredTaskSession(task)
    foreign = uuid4()
    with pytest.raises(NotFoundError):
        getattr(PlatformTaskRepository(session), method)(
            task.id, expected_tenant_id=foreign, **kwargs
        )
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert f"app.task.tenant_id = '{foreign}'" in sql
    assert not session.writes


def test_unregistered_worker_kind_cannot_even_sweep_other_work():
    session = StoredTaskSession()
    with pytest.raises(ValidationError):
        PlatformTaskRepository(session).claim_next_any_tenant(kind="unknown", kinds=("known",))
    assert not session.statements


def test_live_leased_intent_is_not_acknowledged_before_parent_stop():
    ctx = _ctx()
    task = _task(ctx, cancel_requested_at=NOW)
    session = StoredTaskSession(rows=[task])
    module._sweep_unexecutable(session, now=NOW, kinds=("known",), tenant_id=ctx.tenant_id)
    assert task.status == "leased"
    assert task.finished_at is None
    assert not session.writes


def test_deadline_crossed_during_claim_query_finishes_without_new_attempt():
    task = _task(status="retryable", lease_until=None, deadline_at=NOW)
    session = StoredTaskSession(task)
    assert module._claim_selected(session, task, lease_seconds=300) is None
    assert task.status == "timed_out"
    assert task.attempts == 1
    assert task.finished_at == NOW
