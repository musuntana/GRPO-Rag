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
import sys
from pathlib import Path

from ragos.core.config import Settings, get_settings
from ragos.core.errors import ValidationError
from ragos.core.logging import configure_logging, get_logger
from ragos.runtime.tasks import (
    TaskCommand,
    TaskHandlerRegistry,
    process_one_task,
)
from ragos.runtime.worker_embedding import (
    HASH_TEST_MODE,
    LOCAL_GATEWAY_MODE,
    validate_worker_embedding_settings,
)
from ragos.storage.db import (
    ensure_schemas,
    get_engine,
    get_session_factory,
    require_migrated_schema,
)

LOGGER = get_logger(__name__)

POLL_IDLE_INTERVAL_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 900

def _base_environment(settings: Settings) -> dict[str, str]:
    return {
        "RAGOS_PG_DSN": settings.pg_dsn,
        "RAGOS_QDRANT_URL": settings.qdrant_url,
        "RAGOS_STORAGE_ROOT": str(Path(settings.storage_root).resolve()),
        "RAGOS_EMBEDDING_PROFILE": settings.embedding_profile,
        "RAGOS_EMBEDDING_VERSION": settings.embedding_version,
    }


def _environment(settings: Settings, mode: str) -> dict[str, str]:
    # Configuration is server-owned. Do not copy ambient env or values from a task payload.
    environment = _base_environment(settings)
    if mode == HASH_TEST_MODE:
        environment.update(
            {
                "RAGOS_APP_ENV": "test",
                "RAGOS_TASK_TEST_EMBEDDING": "true",
            }
        )
        return environment
    if mode == LOCAL_GATEWAY_MODE:
        if settings.local_model_provider_id is None:
            raise RuntimeError("development ingest requires an admitted local model provider")
        environment.update(
            {
                "RAGOS_APP_ENV": "dev",
                "RAGOS_INGEST_LOCAL_MODEL_ENABLED": "true",
                "RAGOS_LOCAL_MODEL_ENABLED": "true",
                "RAGOS_LOCAL_MODEL_PROVIDER_ID": str(settings.local_model_provider_id),
                "RAGOS_LOCAL_MODEL_BASE_URL": settings.local_model_base_url,
                "RAGOS_LOCAL_MODEL_CHAT": settings.local_model_chat,
                "RAGOS_LOCAL_MODEL_EMBED": settings.local_model_embed,
                "RAGOS_LOCAL_MODEL_EMBED_REVISION": settings.local_model_embed_revision,
                "RAGOS_LOCAL_MODEL_EMBED_DIMENSION": str(
                    settings.local_model_embed_dimension
                ),
                "RAGOS_API_HOST": settings.api_host,
            }
        )
        return environment
    raise RuntimeError("ingest embedding mode is unavailable")


def build_registry(settings: Settings) -> TaskHandlerRegistry:
    try:
        mode = validate_worker_embedding_settings(settings)
    except ValidationError as exc:
        raise RuntimeError(str(exc)) from exc
    if not settings.qdrant_url:
        raise RuntimeError("ingest worker requires an explicit vector service")
    environment = _environment(settings, mode)
    registry = TaskHandlerRegistry()

    def command(*, task, tenant_ctx):
        if task.tenant_id != tenant_ctx.tenant_id or task.kind != "ingest_document":
            raise ValueError("task command scope mismatch")
        return TaskCommand(
            (
                sys.executable,
                "-m",
                "ragos.runtime.ingest_job",
                "--tenant-id",
                str(task.tenant_id),
                "--task-id",
                str(task.id),
                "--attempt",
                str(task.attempts),
            ),
            environment,
            replay_safe=True,
        )

    registry.register("ingest_document", command)
    return registry


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
                shutdown=stop_event.is_set,
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
    registry = build_registry(settings)
    engine = get_engine(settings)
    require_migrated_schema(engine, settings)
    ensure_schemas(engine, settings)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    LOGGER.info(
        "ingest worker initialized",
        extra={"registered_kinds": list(registry.kinds)},
    )
    await _poll_loop(settings, stop_event, registry)


def run() -> None:
    asyncio.run(serve())
