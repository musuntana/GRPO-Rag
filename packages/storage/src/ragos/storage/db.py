from __future__ import annotations

from collections.abc import Iterator

import ragos.storage.models  # noqa: F401
from ragos.core.config import Settings, get_settings
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, create_engine

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    cfg = settings or get_settings()
    return create_engine(
        cfg.sqlalchemy_sync_dsn,
        echo=cfg.pg_echo,
        pool_pre_ping=True,
        pool_size=cfg.pg_pool_size,
        max_overflow=cfg.pg_max_overflow,
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
