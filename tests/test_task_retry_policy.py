from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from ragos.storage.models.app import Task
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.tasks import (
    BASE_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    _apply_failure,
    compute_retry_delay,
)


def _task(attempts: int) -> Task:
    return Task(
        id=uuid4(),
        tenant_id=uuid4(),
        kind="demo",
        payload={},
        idempotency_key=f"idem-{uuid4()}",
        attempts=attempts,
        status="leased",
        lease_until=utc_now() + timedelta(seconds=60),
        deadline_at=utc_now() + timedelta(seconds=300),
        created_at=utc_now(),
        updated_at=utc_now(),
    )


@pytest.mark.parametrize(
    ("attempts", "expected_seconds"),
    [
        (1, BASE_BACKOFF_SECONDS),
        (2, BASE_BACKOFF_SECONDS * 2),
        (3, BASE_BACKOFF_SECONDS * 4),
        (10, MAX_BACKOFF_SECONDS),  # capped
        (0, BASE_BACKOFF_SECONDS),  # defensive
    ],
)
def test_compute_retry_delay_exponential_with_cap(
    attempts: int,
    expected_seconds: int,
) -> None:
    delay = compute_retry_delay(attempts)
    assert delay == timedelta(seconds=expected_seconds)


def test_apply_failure_retryable_pushes_available_at_forward() -> None:
    task = _task(attempts=1)
    before = utc_now()

    _apply_failure(task, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS)

    assert task.status == "retryable"
    assert task.lease_until is None
    assert task.available_at is not None
    # Should be pushed forward by at least the base backoff.
    assert task.available_at >= before + timedelta(seconds=BASE_BACKOFF_SECONDS - 1)


def test_apply_failure_demotes_to_failed_after_cap() -> None:
    task = _task(attempts=DEFAULT_MAX_ATTEMPTS)

    _apply_failure(task, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS)

    assert task.status == "failed"
    assert task.lease_until is None


def test_apply_failure_non_retryable_immediately_fails() -> None:
    task = _task(attempts=1)

    _apply_failure(task, retryable=False, max_attempts=DEFAULT_MAX_ATTEMPTS)

    assert task.status == "failed"
    assert task.lease_until is None


def test_apply_failure_backoff_grows_with_attempts() -> None:
    task_a = _task(attempts=1)
    task_b = _task(attempts=3)
    now = utc_now()

    _apply_failure(task_a, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS)
    _apply_failure(task_b, retryable=True, max_attempts=DEFAULT_MAX_ATTEMPTS)

    assert task_a.available_at is not None
    assert task_b.available_at is not None
    delay_a = task_a.available_at - now
    delay_b = task_b.available_at - now
    assert delay_b > delay_a


def test_non_retryable_never_sets_available_at() -> None:
    task = _task(attempts=1)
    original_available = datetime.fromtimestamp(0, tz=task.created_at.tzinfo)
    task.available_at = original_available

    _apply_failure(task, retryable=False, max_attempts=DEFAULT_MAX_ATTEMPTS)

    assert task.available_at == original_available


@pytest.mark.parametrize("status,attempts", [("completed", 1), ("leased", 2), ("failed", 1)])
def test_superseded_claim_cannot_complete(status: str, attempts: int) -> None:
    from ragos.core.errors import ConflictError
    from ragos.storage.repositories.tasks import _require_claim

    task = _task(attempts=attempts)
    task.status = status
    with pytest.raises(ConflictError):
        _require_claim(task, expected_attempts=1)


def test_expired_leased_claim_is_eligible_and_unknown_kind_filtered() -> None:
    from ragos.storage.repositories.tasks import _claim_statement
    from sqlalchemy.dialects import postgresql

    statement = _claim_statement(kind=None, kinds=("ingest_document",), now=utc_now())
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "app.task.status = 'leased'" in sql
    assert "app.task.kind IN ('ingest_document')" in sql
    assert "SKIP LOCKED" in sql
