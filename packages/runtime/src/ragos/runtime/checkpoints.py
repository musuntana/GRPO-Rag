from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ragos.core.config import Settings, get_settings

if TYPE_CHECKING:
    pass


def build_checkpoint_dsn(dsn: str, schema: str) -> str:
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    option = f"-csearch_path={schema}"
    existing = query.get("options", "").strip()
    query["options"] = f"{existing} {option}".strip()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@asynccontextmanager
async def langgraph_checkpointer(
    settings: Settings | None = None,
) -> AsyncIterator[Any]:
    cfg = settings or get_settings()
    checkpoint_dsn = build_checkpoint_dsn(cfg.pg_dsn, cfg.pg_checkpoint_schema)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
