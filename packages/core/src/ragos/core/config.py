from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "RAGOS"
    app_env: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    pg_dsn: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/ragos",
        description="Raw PostgreSQL DSN used by psycopg and LangGraph.",
    )
    pg_app_schema: str = "app"
    pg_search_schema: str = "search"
    pg_checkpoint_schema: str = "checkpoint"
    pg_echo: bool = False
    pg_pool_size: int = 10
    pg_max_overflow: int = 20
    storage_root: str = "data/storage"
    qdrant_url: str = "http://localhost:6333"
    valkey_url: str = "redis://localhost:6379"

    model_config = SettingsConfigDict(
        env_prefix="RAGOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def sqlalchemy_sync_dsn(self) -> str:
        if self.pg_dsn.startswith("postgresql+psycopg://"):
            return self.pg_dsn
        if self.pg_dsn.startswith("postgresql://"):
            return self.pg_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        raise ValueError("RAGOS_PG_DSN must start with postgresql:// or postgresql+psycopg://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
