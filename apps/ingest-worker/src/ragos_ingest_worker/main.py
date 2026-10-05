"""Ingest worker process entrypoint.

Thin by design (AGENTS.md: process entrypoints stay thin, reusable logic lives
in shared packages). Ingestion logic is in ``packages/retrieval`` and
``packages/runtime``; this module owns the poll loop and signal handling only.

Ingest is a separate process from chat on purpose: a bulk reindex must not be
able to starve interactive requests (architecture §11.0.4).
"""

from __future__ import annotations

import asyncio
import signal

from ragos.core.config import Settings, get_settings
from ragos.core.logging import configure_logging, get_logger
from ragos.runtime.ingest import NoopVectorIndexWriter, VectorIndexWriter
from ragos.runtime.tasks import (
    TaskHandlerRegistry,
    make_ingest_document_handler,
    process_one_task,
)
from ragos.storage.db import ensure_schemas, get_engine, get_session_factory

LOGGER = get_logger(__name__)

POLL_IDLE_INTERVAL_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 900


def build_registry(*, vector_writer: VectorIndexWriter | None = None) -> TaskHandlerRegistry:
    registry = TaskHandlerRegistry()
    registry.register(
        "ingest_document",
        make_ingest_document_handler(vector_writer=vector_writer),
    )
    return registry


def build_vector_writer(settings: Settings) -> VectorIndexWriter:
    if not settings.qdrant_url:
        LOGGER.info("RAGOS_QDRANT_URL empty — using NoopVectorIndexWriter")
        return NoopVectorIndexWriter()
    from ragos.runtime.vector import QdrantVectorIndexWriter

    return QdrantVectorIndexWriter(
        url=settings.qdrant_url,
        embedding_profile=settings.embedding_profile,
    )


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
        "ingest poll loop started",
        extra={"handlers": list(registry.kinds), "lease_seconds": lease_seconds},
    )
    while not stop_event.is_set():
        try:
            worked = await asyncio.to_thread(
                process_one_task,
                session_factory,
                registry,
                lease_seconds=lease_seconds,
                kind="ingest_document",
            )
        except Exception:  # noqa: BLE001 — the loop must outlive one bad task
            LOGGER.exception("ingest poll iteration crashed")
            worked = False

        if worked:
            continue

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_idle_interval)
        except TimeoutError:
            pass
    LOGGER.info("ingest poll loop exited")


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
            pass

    registry = build_registry(vector_writer=build_vector_writer(settings))
    LOGGER.info(
        "ingest worker initialized",
        extra={"registered_kinds": list(registry.kinds)},
    )
    await _poll_loop(settings, stop_event, registry)


def run() -> None:
    asyncio.run(serve())