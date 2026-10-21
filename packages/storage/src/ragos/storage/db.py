from __future__ import annotations

from collections.abc import Iterator

import ragos.storage.models  # noqa: F401
from ragos.core.config import Settings, get_settings
from sqlalchemy import Engine, column, event, select, table, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.elements import quoted_name
from sqlmodel import Session, create_engine

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
REQUIRED_SCHEMA_REVISION = "0013_chat_request_claims"


def require_migrated_schema(engine: Engine, settings: Settings) -> None:
    """Refuse startup before any setup writes when the installed schema differs.

    The release owns this revision; migrations are an explicit operator action.
    A version marker is a prerequisite, not a replacement for migration checks.
    """
    versions = table(
        "alembic_version",
        column("version_num"),
        schema=quoted_name(settings.pg_app_schema, True),
    )
    try:
        with engine.connect() as connection:
            revisions = connection.execute(select(versions.c.version_num)).scalars().all()
    except SQLAlchemyError:
        raise RuntimeError(
            "database schema unavailable; apply release migrations before startup"
        ) from None
    if revisions != [REQUIRED_SCHEMA_REVISION]:
        raise RuntimeError(
            "database schema does not match this release; apply migrations before startup"
        )


def build_engine(settings: Settings | None = None) -> Engine:
    cfg = settings or get_settings()
    return create_engine(
        cfg.sqlalchemy_sync_dsn,
        echo=cfg.pg_echo,
        pool_pre_ping=True,
        pool_size=cfg.pg_pool_size,
        max_overflow=cfg.pg_max_overflow,
        pool_timeout=cfg.pg_pool_timeout,
        connect_args={
            "connect_timeout": cfg.pg_connect_timeout,
            "options": f"-c statement_timeout={cfg.pg_statement_timeout_ms} "
            f"-c lock_timeout={cfg.pg_lock_timeout_ms}",
        },
    )


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = build_engine(settings)
    return _ENGINE


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(
            bind=get_engine(settings),
            class_=Session,
            expire_on_commit=False,
        )
    return _SESSION_FACTORY


def get_session(settings: Settings | None = None) -> Iterator[Session]:
    session_factory = get_session_factory(settings)
    with session_factory() as session:
        yield session


def ensure_schemas(engine: Engine | None = None, settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    db_engine = engine or get_engine(cfg)

    with db_engine.begin() as connection:
        for schema in (cfg.pg_app_schema, cfg.pg_search_schema, cfg.pg_checkpoint_schema):
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def bind_session_tenant(session: Session, tenant_id) -> None:
    """Set RLS scope in each new transaction without triggering autobegin.

    The caller owns begin/commit. SET LOCAL expires at transaction end and is
    reapplied after rollback or connection-pool reuse for this scoped session.
    """
    from uuid import UUID

    from ragos.core.errors import PermissionDeniedError

    if not isinstance(tenant_id, UUID) or session.in_transaction():
        raise PermissionDeniedError("RLS identity must be bound before the transaction")

    def set_scope(scoped_session, transaction, connection):
        connection.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )

    event.listen(session, "after_begin", set_scope)
