from __future__ import annotations

import asyncio
import signal

from ragos.core.config import Settings, get_settings
from ragos.core.logging import configure_logging, get_logger
from ragos.runtime.checkpoints import langgraph_checkpointer
from ragos.runtime.ingest import NoopVectorIndexWriter, VectorIndexWriter
from ragos.runtime.tasks import (
    TaskHandlerRegistry,
    log_only_handler,
    make_ingest_document_handler,
    process_one_task,
)
from ragos.storage.db import ensure_schemas, get_engine, get_session_factory

LOGGER = get_logger(__name__)

POLL_IDLE_INTERVAL_SECONDS = 1.0
POLL_BUSY_INTERVAL_SECONDS = 0.0
DEFAULT_LEASE_SECONDS = 300


def build_default_registry(
    *,
    vector_writer: VectorIndexWriter | None = None,
) -> TaskHandlerRegistry:
    registry = TaskHandlerRegistry()
    # Stub handler for smoke testing.
    registry.register("log_only", log_only_handler)
    # Real ingest path: FTS dual-write + vector upsert via injected writer.
    registry.register(
        "ingest_document",
        make_ingest_document_handler(vector_writer=vector_writer),
    )
    return registry


def build_vector_writer(settings: Settings) -> VectorIndexWriter:
    """Resolve a vector writer based on configuration.

    Empty ``qdrant_url`` → Noop (tests, CI without qdrant).
    Populated URL → real ``QdrantVectorIndexWriter``. Imported lazily so the
    qdrant-client dependency is not required for unit-level tests.
    """
    if not settings.qdrant_url:
        LOGGER.info("RAGOS_QDRANT_URL empty — using NoopVectorIndexWriter")
        return NoopVectorIndexWriter()
    from ragos.runtime.vector import QdrantVectorIndexWriter

    LOGGER.info("initializing QdrantVectorIndexWriter", extra={"url": settings.qdrant_url})
    return QdrantVectorIndexWriter(url=settings.qdrant_url)


async def _poll_loop(
    settings: Settings,
    stop_event: asyncio.Event,
    registry: TaskHandlerRegistry,
    *,
    poll_idle_interval: float = POLL_IDLE_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    session_factory = get_session_factory(settings)
    LOGGER.info(
        "task poll loop started",
        extra={"handlers": list(registry.kinds), "lease_seconds": lease_seconds},
    )
    while not stop_event.is_set():
        try:
            worked = await asyncio.to_thread(
                process_one_task,
                session_factory,
                registry,
                lease_seconds=lease_seconds,
            )
        except Exception:  # noqa: BLE001 — never let the loop die
            LOGGER.exception("task poll iteration crashed")
            worked = False

        if worked:
            # Immediately try the next task — no sleep.
            continue

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_idle_interval)
        except TimeoutError:
            pass
    LOGGER.info("task poll loop exited")


async def serve() -> None:
    settings = get_settings()
    configure_logging("INFO")

    engine = get_engine(settings)
    ensure_schemas(engine, settings)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Non-Unix platforms, or running inside a runner that already
            # installs signal handlers.
            pass

    vector_writer = build_vector_writer(settings)
    registry = build_default_registry(vector_writer=vector_writer)

    async with langgraph_checkpointer(settings):
        LOGGER.info(
            "workflow worker initialized",
            extra={
                "app_schema": settings.pg_app_schema,
                "search_schema": settings.pg_search_schema,
                "checkpoint_schema": settings.pg_checkpoint_schema,
                "registered_kinds": list(registry.kinds),
            },
        )
        await _poll_loop(settings, stop_event, registry)


def run() -> None:
    asyncio.run(serve())
