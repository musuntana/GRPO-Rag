from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from ragos.core.tenant import ActorType, TenantContext
from ragos.runtime.tasks import (
    ClaimedTaskRef,
    TaskHandlerRegistry,
    _execute_claimed,
    log_only_handler,
    process_one_task,
)
from ragos.storage.models.app import Task


def _fake_task(kind: str = "log_only") -> Task:
    return Task(
        id=uuid4(),
        tenant_id=uuid4(),
        kind=kind,
        payload={"hello": "world"},
        idempotency_key=f"idem-{uuid4()}",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _SessionStub:
    """Minimal context manager standing in for an sqlmodel Session.

    Supports both ``with session_factory() as session`` and
    ``with session.begin()`` nesting patterns used by the dispatcher.
    """

    def __enter__(self) -> _SessionStub:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def begin(self) -> _SessionStub:
        return self


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_resolve() -> None:
    registry = TaskHandlerRegistry()

    def handler(*, task: Task, tenant_ctx: TenantContext, session: Any) -> None:
        return None

    registry.register("ingest", handler)

    assert registry.resolve("ingest") is handler
    assert registry.resolve("missing") is None
    assert "ingest" in registry.kinds


# ---------------------------------------------------------------------------
# log_only_handler
# ---------------------------------------------------------------------------


def test_log_only_handler_noop() -> None:
    task = _fake_task()
    ctx = TenantContext(
        tenant_id=task.tenant_id,
        tenant_slug="acme",
        actor_id="worker",
        actor_type=ActorType.SERVICE,
        roles=["service"],
        trace_id="trace-1",
    )
    log_only_handler(task=task, tenant_ctx=ctx, session=object())


# ---------------------------------------------------------------------------
# process_one_task — wiring between _claim_one and _execute_claimed
# ---------------------------------------------------------------------------


def test_process_one_task_returns_false_when_claim_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ragos.runtime.tasks._claim_one", lambda *a, **kw: None)

    called = False

    def _unused_execute(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("ragos.runtime.tasks._execute_claimed", _unused_execute)

    registry = TaskHandlerRegistry()
    assert process_one_task(lambda: _SessionStub(), registry) is False
    assert called is False


def test_process_one_task_dispatches_claimed_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = ClaimedTaskRef(id=uuid4(), tenant_id=uuid4(), kind="demo")
    monkeypatch.setattr("ragos.runtime.tasks._claim_one", lambda *a, **kw: ref)

    captured: dict[str, Any] = {}

    def fake_execute(factory, ref_arg, registry_arg):
        captured["ref"] = ref_arg
        captured["registry"] = registry_arg

    monkeypatch.setattr("ragos.runtime.tasks._execute_claimed", fake_execute)

    registry = TaskHandlerRegistry()
    assert process_one_task(lambda: _SessionStub(), registry) is True
    assert captured["ref"] is ref
    assert captured["registry"] is registry


# ---------------------------------------------------------------------------
# _execute_claimed — unregistered kind path
# ---------------------------------------------------------------------------


def test_execute_claimed_missing_handler_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = ClaimedTaskRef(id=uuid4(), tenant_id=uuid4(), kind="never_registered")
    registry = TaskHandlerRegistry()

    calls: list[tuple[str, UUID, bool | None]] = []

    class _Recorder:
        def __init__(self, session: Any) -> None:
            self.session = session

        def mark_failed(self, task_id: UUID, *, retryable: bool) -> None:
            calls.append(("failed", task_id, retryable))

        def mark_completed(self, task_id: UUID) -> None:  # pragma: no cover
            calls.append(("completed", task_id, None))

    monkeypatch.setattr("ragos.runtime.tasks.PlatformTaskRepository", _Recorder)

    _execute_claimed(lambda: _SessionStub(), ref, registry)

    assert calls == [("failed", ref.id, False)]
