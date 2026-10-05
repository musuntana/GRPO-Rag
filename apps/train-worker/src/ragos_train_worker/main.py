"""Train worker process. Thin entrypoint; logic lives in shared packages.

Offline reranker training (ADR-0007) runs here, never on a request path and
never on the ingest worker. The separation is the same argument as execution
isolation versus network isolation (ADR-0003): a latency-budgeted call and
minutes-to-hours of compute must not share a process.
"""

from __future__ import annotations

import signal

from ragos.core.config import Settings, get_settings
from ragos.core.logging import configure_logging, get_logger
from ragos.runtime.tasks import (
    TaskHandlerRegistry,
    make_train_reranker_handler,
    process_one_task,
)
from ragos.storage.db import ensure_schemas, get_engine, get_session_factory

LOGGER = get_logger(__name__)

POLL_IDLE_INTERVAL_SECONDS = 5.0
DEFAULT_LEASE_SECONDS = 3600


def build_registry(*, settings: Settings | None = None) -> TaskHandlerRegistry:
    registry = TaskHandlerRegistry()
    registry.register("train_reranker", make_train_reranker_handler())
    return registry


async def _poll_loop(
    settings: Settings,
    stop_event,
    registry: TaskHandlerRegistry,
    *,
    poll_idle_interval: float = POLL_IDLE_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    import asyncio

    session_factory = get_session_factory(settings)
    LOGGER.info(
        "train poll loop started",
        extra={"handlers": list(registry.kinds), "lease_seconds": lease_seconds},
    )
    while not stop_event.is_set():
        try:
            worked = await asyncio.to_thread(
                process_one_task,
                session_factory,
                registry,
                lease_seconds=lease_seconds,
                kind="train_reranker",
            )
        except Exception:  # noqa: BLE001 — the loop must outlive one bad run
            LOGGER.exception("train poll iteration crashed")
            worked = False

        if worked:
            continue

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_idle_interval)
        except TimeoutError:
            pass
    LOGGER.info("train poll loop exited")


async def serve() -> None:
    import asyncio

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

    registry = build_registry(settings=settings)
    LOGGER.info(
        "train worker initialized",
        extra={"registered_kinds": list(registry.kinds)},
    )
    await _poll_loop(settings, stop_event, registry)


def run() -> None:
    import asyncio

    asyncio.run(serve())


__all__ = ["run"]