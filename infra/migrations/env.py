from __future__ import annotations

from logging.config import fileConfig

import ragos.storage.models  # noqa: F401
from alembic import context
from ragos.core.config import get_settings
from ragos.storage.models import RagosModel
from sqlalchemy import engine_from_config, pool, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_sync_dsn)

target_metadata = RagosModel.metadata


def include_name(name, type_, parent_names):
    # LangGraph manages checkpoint tables through saver.setup(); Alembic owns app/search only.
    if type_ == "schema":
        return name in {settings.pg_app_schema, settings.pg_search_schema}
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        literal_binds=True,
        compare_type=True,
        version_table_schema=settings.pg_app_schema,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        for schema in (
            settings.pg_app_schema,
            settings.pg_search_schema,
            settings.pg_checkpoint_schema,
        ):
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
            version_table_schema=settings.pg_app_schema,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
