from __future__ import annotations

from functools import lru_cache
from uuid import UUID

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
    pg_connect_timeout: int = Field(default=3, ge=1, le=30)
    pg_pool_timeout: int = Field(default=3, ge=1, le=30)
    pg_statement_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    pg_lock_timeout_ms: int = Field(default=1000, ge=1, le=10000)
    storage_root: str = "data/storage"
    embedding_profile: str = "hash-test"
    embedding_version: str = "sha256-hash-v1"
    task_test_embedding: bool = False
    qdrant_url: str = ""
    valkey_url: str = "redis://localhost:6379"

    # Explicit non-production LOCAL MODEL ONLY; disabled by default.
    # No tenant-entered arbitrary model URLs or decrypted key-pool secrets.
    local_model_enabled: bool = False
    # Separate opt-in for the trusted ingest worker real-gateway path.
    ingest_local_model_enabled: bool = False
    local_model_provider_id: UUID | None = None
    local_model_base_url: str = ""
    local_model_chat: str = ""
    local_model_embed: str = ""
    # Operator-recorded revision; not independently attested by the gateway.
    local_model_embed_revision: str = ""
    local_model_embed_dimension: int = Field(default=0, ge=0, le=8192)

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
