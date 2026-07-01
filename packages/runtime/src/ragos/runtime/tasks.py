from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ragos.core.errors import NotFoundError
from ragos.core.logging import get_logger
from ragos.core.tenant import ActorType, TenantContext
from ragos.runtime.ingest import IngestionService, VectorIndexWriter
from ragos.storage.models.app import Task, Tenant
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.search import SearchChunkUpsert, SearchTier
from ragos.storage.repositories.tasks import PlatformTaskRepository, TaskRepository
from sqlalchemy.orm import sessionmaker
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
    ) -> Task:
        with self.session.begin():
            task = self.repo.enqueue(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                available_at=available_at,
            )
        return task

    def claim_next(self, *, lease_seconds: int = 300, kind: str | None = None) -> Task | None:
        with self.session.begin():
            return self.repo.claim_next(lease_seconds=lease_seconds, kind=kind)

    def mark_completed(self, task_id: UUID) -> Task:
        with self.session.begin():
            return self.repo.mark_completed(task_id)

    def mark_failed(self, task_id: UUID, *, retryable: bool) -> Task:
        with self.session.begin():
            return self.repo.mark_failed(task_id, retryable=retryable)


# ---------------------------------------------------------------------------
# Worker-side task dispatch
# ---------------------------------------------------------------------------


class TaskHandler(Protocol):
    """Callable invoked by the worker for a claimed task.

    Handlers run inside an open transaction on ``session``; the dispatcher
    commits the enclosing tx and marks the task ``completed`` if the handler
    returns normally, or ``retryable`` on exception.
    """

    def __call__(
        self,
        *,
        task: Task,
        tenant_ctx: TenantContext,
        session: Session,
    ) -> None: ...


class TaskHandlerRegistry:
    """Maps task ``kind`` to a handler callable."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, kind: str, handler: TaskHandler) -> None:
        self._handlers[kind] = handler

    def resolve(self, kind: str) -> TaskHandler | None:
        return self._handlers.get(kind)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._handlers.keys())


def build_worker_tenant_ctx(
    tenant_id: UUID,
    session: Session,
    *,
    trace_id: str,
    actor_id: str = "workflow-worker",
) -> TenantContext:
    """Construct a service-level TenantContext for a worker run.

    The worker path mirrors the canonical tenant policy (plan tier, locale,
    timezone, security level) off the ``app.tenant`` row so background
    behaviour — quota, model routing, localization, redaction — stays aligned
    with the request path. Only ``actor_type`` / ``roles`` differ because the
    caller is a service identity, not an end user.
    """
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError(
            "Tenant not found",
            resource="Tenant",
            id=str(tenant_id),
        )

    security_level = tenant.security_level
    if security_level not in _VALID_SECURITY_LEVELS:
        LOGGER.warning(
            "tenant.security_level out of range; falling back to 'internal'",
            extra={
                "tenant_id": str(tenant_id),
                "observed": security_level,
            },
        )
        security_level = "internal"

    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug=tenant.slug,
        actor_id=actor_id,
        actor_type=ActorType.SERVICE,
        roles=["service"],
        plan_tier=tenant.plan_tier,
        locale=tenant.locale,
        timezone=tenant.timezone,
        security_level=security_level,  # type: ignore[arg-type]
        trace_id=trace_id,
    )


@dataclass(slots=True)
class ClaimedTaskRef:
    """Snapshot of task identity taken immediately after claim commits."""

    id: UUID
    tenant_id: UUID
    kind: str


def _claim_one(
    session_factory: Callable[[], Session],
    *,
    lease_seconds: int,
    kind: str | None,
) -> ClaimedTaskRef | None:
    """Tx1 — claim and commit the lease. Row lock is released on commit."""
    with session_factory() as session:
        with session.begin():
            repo = PlatformTaskRepository(session)
            task = repo.claim_next_any_tenant(lease_seconds=lease_seconds, kind=kind)
            if task is None:
                return None
            return ClaimedTaskRef(id=task.id, tenant_id=task.tenant_id, kind=task.kind)


def _execute_claimed(
    session_factory: Callable[[], Session],
    ref: ClaimedTaskRef,
    registry: TaskHandlerRegistry,
) -> None:
    """Tx2 — run the handler and mark completed, or tx3 — mark failed.

    The handler tx is opened explicitly *first* so that tenant + task lookups
    and the handler work all participate in the same transaction. Opening
    ``session.begin()`` after a read would raise ``InvalidRequestError`` because
    the session autobegins on the first query.
    """
    handler = registry.resolve(ref.kind)
    if handler is None:
        LOGGER.warning(
            "no task handler registered; marking failed",
            extra={"task_id": str(ref.id), "kind": ref.kind},
        )
        with session_factory() as session:
            with session.begin():
                PlatformTaskRepository(session).mark_failed(ref.id, retryable=False)
        return

    try:
        with session_factory() as session:
            with session.begin():
                tenant_ctx = build_worker_tenant_ctx(
                    ref.tenant_id,
                    session,
                    trace_id=f"worker-{ref.id}",
                )
                task_obj = session.get(Task, ref.id)
                if task_obj is None:
                    raise NotFoundError(
                        "Task not found",
                        resource="Task",
                        id=str(ref.id),
                    )
                handler(task=task_obj, tenant_ctx=tenant_ctx, session=session)
                PlatformTaskRepository(session).mark_completed(ref.id)
    except Exception:
        LOGGER.exception(
            "task handler raised; marking retryable",
            extra={"task_id": str(ref.id), "kind": ref.kind},
        )
        # New session — the previous one is in a rolled-back / dirty state.
        with session_factory() as session:
            with session.begin():
                PlatformTaskRepository(session).mark_failed(ref.id, retryable=True)


def process_one_task(
    session_factory: Callable[[], Session] | sessionmaker[Session],
    registry: TaskHandlerRegistry,
    *,
    lease_seconds: int = 300,
    kind: str | None = None,
) -> bool:
    """Claim and execute one task. Returns True if a task was processed."""
    factory: Callable[[], Session] = session_factory  # type: ignore[assignment]
    ref = _claim_one(factory, lease_seconds=lease_seconds, kind=kind)
    if ref is None:
        return False
    _execute_claimed(factory, ref, registry)
    return True


def log_only_handler(
    *,
    task: Task,
    tenant_ctx: TenantContext,
    session: Session,
) -> None:
    """Default stub handler used for smoke tests — just logs."""
    LOGGER.info(
        "log_only handler executed",
        extra={
            "task_id": str(task.id),
            "kind": task.kind,
            "tenant_slug": tenant_ctx.tenant_slug,
            "payload_keys": list(task.payload.keys()) if task.payload else [],
        },
    )


# ---------------------------------------------------------------------------
# ingest_document handler
# ---------------------------------------------------------------------------


def _parse_optional_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def decode_chunk_upsert(
    raw: dict[str, Any],
    tenant_ctx: TenantContext,
) -> SearchChunkUpsert:
    """Deserialize a JSON chunk payload into a SearchChunkUpsert.

    The handler fixes ``tenant_id`` to the worker's tenant context so a task
    payload cannot carry a chunk targeted at a different tenant.
    """
    return SearchChunkUpsert(
        id=UUID(str(raw["id"])),
        tenant_id=tenant_ctx.tenant_id,
        kb_id=_parse_optional_uuid(raw.get("kb_id")),
        document_id=_parse_optional_uuid(raw.get("document_id")),
        parent_chunk_id=_parse_optional_uuid(raw.get("parent_chunk_id")),
        layer=raw.get("layer", "L1"),
        lang=raw.get("lang", "simple"),
        title=raw.get("title"),
        headings_text=raw.get("headings_text"),
        body=raw["body"],
        tags_text=raw.get("tags_text"),
        metadata_json=raw.get("metadata_json") or {},
        updated_at=_parse_optional_datetime(raw.get("updated_at")) or utc_now(),
        expires_at=_parse_optional_datetime(raw.get("expires_at")),
    )


def make_ingest_document_handler(
    vector_writer: VectorIndexWriter | None = None,
) -> TaskHandler:
    """Build an ``ingest_document`` handler bound to a vector writer.

    Expected task payload::

        {
            "manifest_id": "<uuid>",
            "tier": "stable" | "hot" | "memory",
            "chunks": [ {id, body, kb_id, document_id, ...}, ... ]
        }

    The handler runs inside the worker dispatcher's open transaction; it does
    NOT start a new one. ``IngestionService.index_chunks`` follows the same
    rule, so the entire FTS + vector dual-write is atomic with the
    ``app.task`` state transition.
    """

    def _handler(
        *,
        task: Task,
        tenant_ctx: TenantContext,
        session: Session,
    ) -> None:
        payload = task.payload or {}
        try:
            manifest_id = UUID(str(payload["manifest_id"]))
            tier = SearchTier(payload.get("tier", SearchTier.STABLE.value))
            raw_chunks = payload.get("chunks") or []
        except (KeyError, ValueError) as exc:
            raise ValueError(f"ingest_document payload invalid: {exc}") from exc

        chunks = [decode_chunk_upsert(c, tenant_ctx) for c in raw_chunks]

        service = IngestionService(
            session=session,
            tenant_ctx=tenant_ctx,
            vector_writer=vector_writer,
        )
        result = service.index_chunks(
            manifest_id=manifest_id,
            tier=tier,
            chunks=chunks,
        )
        LOGGER.info(
            "ingest_document handler executed",
            extra={
                "task_id": str(task.id),
                "manifest_id": str(result.manifest_id),
                "chunk_total": result.chunk_total,
                "tier": tier.value,
                "tenant_slug": tenant_ctx.tenant_slug,
            },
        )

    return _handler
