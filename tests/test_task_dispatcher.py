"""Dispatcher mock-state contracts; process kill and PG behavior have separate evidence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError as SchemaError
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime import tasks
from ragos.runtime.process_runner import TaskProcessResult
from ragos.storage.models.base import utc_now


def ref():
    return tasks.ClaimedTaskRef(
        id=uuid4(),
        tenant_id=uuid4(),
        kind="ingest_document",
        attempts=1,
        lease_until=utc_now() + timedelta(seconds=30),
        deadline_at=utc_now() + timedelta(seconds=60),
    )


def test_envelope_cannot_change_identity_or_payload_by_nested_mutation():
    claimed = ref()
    envelope = tasks.TaskEnvelope(**claimed.model_dump(), payload_json='{"manifest_id":"value"}')
    payload = envelope.payload
    payload["manifest_id"] = "other"
    assert envelope.payload["manifest_id"] == "value"
    with pytest.raises(SchemaError):
        envelope.attempts = 2


def test_registry_single_owner_and_known_kind_only():
    registry = tasks.TaskHandlerRegistry()

    def command(**kwargs):
        return tasks.TaskCommand(("/trusted/task",), {})

    registry.register("ingest_document", command)
    with pytest.raises(ValidationError):
        registry.register("ingest_document", command)
    assert registry.resolve("unknown") is None


def test_process_one_task_never_claims_an_unregistered_kind(monkeypatch):
    monkeypatch.setattr(tasks, "_claim_one", lambda *args, **kwargs: pytest.fail("must not claim"))
    with pytest.raises(ValidationError, match="not registered"):
        tasks.process_one_task(lambda: None, tasks.TaskHandlerRegistry(), kind="train_reranker")


def test_empty_registry_does_not_open_a_database():
    assert not tasks.process_one_task(
        lambda: pytest.fail("no database"), tasks.TaskHandlerRegistry()
    )


@pytest.mark.parametrize(
    "error_code,exit_code,expected",
    [
        (None, 0, "completed"),
        ("TASK_PROCESS_FAILED", 1, "failed"),
        ("TASK_FENCE_LOST", None, "failed"),
    ],
)
def test_dispatch_releases_transaction_before_process_and_terminal_update(
    monkeypatch, error_code, exit_code, expected
):
    claimed = ref()
    row = SimpleNamespace(
        **claimed.model_dump(),
        payload={"manifest_id": str(uuid4())},
        status="leased",
        cancel_requested_at=None,
    )
    state = {"in_tx": False, "terminal": None, "renewed": 0}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @contextmanager
        def begin(self):
            state["in_tx"] = True
            try:
                yield
            finally:
                state["in_tx"] = False

        def get(self, *args):
            return row

    class Repo:
        def __init__(self, session):
            pass

        def lock_claim(self, *args, **kwargs):
            assert kwargs["expected_tenant_id"] == claimed.tenant_id
            assert state["in_tx"]
            return row

        def renew_lease(self, *args, **kwargs):
            state["renewed"] += 1

        def mark_completed(self, *args, **kwargs):
            state["terminal"] = "completed"

        def mark_failed(self, *args, **kwargs):
            state["terminal"] = "failed"

    class Runner:
        def run(self, argv, **kwargs):
            assert not state["in_tx"]
            kwargs["heartbeat"]()
            assert not kwargs["cancellation"]()
            assert not state["in_tx"]
            return TaskProcessResult(exit_code, error_code)

    monkeypatch.setattr(tasks, "bind_session_tenant", lambda *args: None)
    monkeypatch.setattr(tasks, "PlatformTaskRepository", Repo)
    context = TenantContext(
        tenant_id=claimed.tenant_id, tenant_slug="test", actor_id="worker", trace_id="test"
    )
    monkeypatch.setattr(tasks, "build_worker_tenant_ctx", lambda *args, **kwargs: context)
    registry = tasks.TaskHandlerRegistry()

    def command(**kwargs):
        assert not state["in_tx"]
        assert isinstance(kwargs["task"], tasks.TaskEnvelope)
        return tasks.TaskCommand(("/trusted/task",), {})

    registry.register(claimed.kind, command)
    tasks._execute_claimed(Session, claimed, registry, runner=Runner(), lease_seconds=10)
    assert state["terminal"] == expected
    assert state["renewed"] == 1


def test_permanent_preparation_failure_records_failed_without_process(monkeypatch):
    claimed = ref()
    recorded = []

    @contextmanager
    def transaction(*args):
        yield object()

    class Repo:
        def __init__(self, *args):
            pass

        def lock_claim(self, *args, **kwargs):
            return SimpleNamespace(kind=claimed.kind)

    monkeypatch.setattr(tasks, "_scoped_transaction", transaction)
    monkeypatch.setattr(tasks, "PlatformTaskRepository", Repo)

    def inactive(*args, **kwargs):
        raise ValidationError("inactive tenant")

    monkeypatch.setattr(tasks, "build_worker_tenant_ctx", inactive)
    monkeypatch.setattr(
        tasks, "_record_terminal", lambda factory, ref, result, **kwargs: recorded.append(result)
    )
    tasks._execute_claimed(lambda: None, claimed, tasks.TaskHandlerRegistry())
    assert recorded[0].error_code == "TASK_FAILED"
