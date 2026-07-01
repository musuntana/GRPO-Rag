from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from ragos.storage.models.base import RagosModel, Timestamped, UUIDPrimaryKey
from sqlalchemy import Column, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

APP_SCHEMA = "app"


class AppTenantScoped(SQLModel):
    tenant_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.tenant.id", nullable=False, index=True)


class Tenant(UUIDPrimaryKey, Timestamped, RagosModel, table=True):
    __tablename__ = "tenant"
    __table_args__ = (UniqueConstraint("slug", name="uq_tenant_slug"), {"schema": APP_SCHEMA})

    slug: str = Field(index=True, max_length=64)
    display_name: str = Field(max_length=255)
    is_active: bool = Field(default=True, nullable=False)
    # Canonical policy fields — mirrored into TenantContext on every request
    # (and every worker-side task run) so background behaviour stays aligned
    # with the request path.
    plan_tier: str = Field(default="standard", max_length=32, nullable=False)
    locale: str = Field(default="zh-CN", max_length=16, nullable=False)
    timezone: str = Field(default="Asia/Shanghai", max_length=64, nullable=False)
    security_level: str = Field(default="internal", max_length=16, nullable=False)


class KnowledgeBase(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_knowledge_base_tenant_slug"),
        {"schema": APP_SCHEMA},
    )

    slug: str = Field(max_length=64)
    name: str = Field(max_length=255)
    description: str | None = Field(default=None)


class DocumentRecord(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "document_record"
    __table_args__ = {"schema": APP_SCHEMA}

    kb_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.knowledge_base.id", nullable=False, index=True)
    external_id: str | None = Field(default=None, max_length=255)
    title: str = Field(max_length=255)
    source_uri: str | None = Field(default=None, max_length=1024)
    ingest_status: str = Field(default="pending", max_length=32)


class WorkflowDefinition(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "workflow_definition"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_workflow_definition_tenant_slug"),
        {"schema": APP_SCHEMA},
    )

    slug: str = Field(max_length=64)
    name: str = Field(max_length=255)
    description: str | None = Field(default=None)


class WorkflowVersion(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "workflow_version"
    __table_args__ = (
        UniqueConstraint("workflow_definition_id", "version", name="uq_workflow_version_number"),
        {"schema": APP_SCHEMA},
    )

    workflow_definition_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.workflow_definition.id",
        nullable=False,
        index=True,
    )
    version: int = Field(nullable=False)
    is_active: bool = Field(default=False, nullable=False)
    dsl: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


class Conversation(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "conversation"
    __table_args__ = {"schema": APP_SCHEMA}

    external_user_id: str = Field(max_length=255, index=True)
    title: str | None = Field(default=None, max_length=255)
    status: str = Field(default="active", max_length=32)


class MessageIndex(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "message_index"
    __table_args__ = {"schema": APP_SCHEMA}

    conversation_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.conversation.id",
        nullable=False,
        index=True,
    )
    role: str = Field(max_length=32)
    content: str = Field(nullable=False)
    token_count: int | None = Field(default=None)
    is_summary: bool = Field(default=False, nullable=False)


class MemorySummary(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "memory_summary"
    __table_args__ = {"schema": APP_SCHEMA}

    conversation_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.conversation.id",
        nullable=False,
        index=True,
    )
    summary: str = Field(nullable=False)


class MemoryFact(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "memory_fact"
    __table_args__ = {"schema": APP_SCHEMA}

    conversation_id: UUID | None = Field(
        default=None,
        foreign_key=f"{APP_SCHEMA}.conversation.id",
        index=True,
    )
    category: str = Field(max_length=64)
    fact: str = Field(nullable=False)
    confidence: float = Field(default=0.5, nullable=False)
    decay_score: float = Field(default=1.0, nullable=False)
    last_accessed_at: datetime | None = Field(default=None)


class ChunkManifest(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "chunk_manifest"
    __table_args__ = {"schema": APP_SCHEMA}

    kb_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.knowledge_base.id", nullable=False, index=True)
    document_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.document_record.id",
        nullable=False,
        index=True,
    )
    chunk_total: int = Field(default=0, nullable=False)
    indexing_state: str = Field(default="pending", max_length=32)
    error_message: str | None = Field(default=None)


class Run(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "run"
    __table_args__ = {"schema": APP_SCHEMA}

    workflow_version_id: UUID | None = Field(
        default=None,
        foreign_key=f"{APP_SCHEMA}.workflow_version.id",
        index=True,
    )
    thread_id: str = Field(max_length=255, index=True)
    status: str = Field(default="queued", max_length=32)


class RunSummary(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "run_summary"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_summary_run_id"),
        {"schema": APP_SCHEMA},
    )

    run_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.run.id", nullable=False, index=True)
    outcome: str | None = Field(default=None, max_length=64)
    latency_ms: int | None = Field(default=None)
    model_usage: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


class Task(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_tenant_idempotency_key"),
        {"schema": APP_SCHEMA},
    )

    kind: str = Field(max_length=64)
    status: str = Field(default="pending", max_length=32, index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    available_at: datetime | None = Field(default=None, index=True)
    lease_until: datetime | None = Field(default=None, index=True)
    attempts: int = Field(default=0, nullable=False)
    idempotency_key: str = Field(max_length=255)


class User(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "user"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_user_tenant_external"),
        {"schema": APP_SCHEMA},
    )

    external_id: str = Field(max_length=255)
    display_name: str = Field(max_length=255)
    email: str | None = Field(default=None, max_length=255)
    roles: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    is_active: bool = Field(default=True, nullable=False)


class TenantDomain(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "tenant_domain"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_tenant_domain_domain"),
        {"schema": APP_SCHEMA},
    )

    domain: str = Field(max_length=255)
    is_verified: bool = Field(default=False, nullable=False)
    is_primary: bool = Field(default=False, nullable=False)


class TenantBrand(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "tenant_brand"
    __table_args__ = {"schema": APP_SCHEMA}

    brand_name: str = Field(max_length=255)
    logo_url: str | None = Field(default=None, max_length=1024)
    primary_color: str | None = Field(default=None, max_length=7)
    css_variables: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


# ---------------------------------------------------------------------------
# Model Gateway — provider / key pool / profile / tenant binding
# ---------------------------------------------------------------------------


class Provider(UUIDPrimaryKey, Timestamped, RagosModel, table=True):
    """LLM provider definition — platform-level, not tenant-scoped."""

    __tablename__ = "provider"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_provider_slug"),
        {"schema": APP_SCHEMA},
    )

    slug: str = Field(max_length=64)
    provider_type: str = Field(max_length=32)  # openai, anthropic
    display_name: str = Field(max_length=255)
    base_url: str | None = Field(default=None, max_length=512)
    is_active: bool = Field(default=True, nullable=False)
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


class KeyPool(UUIDPrimaryKey, Timestamped, RagosModel, table=True):
    """API key pool — weighted round-robin with health tracking."""

    __tablename__ = "key_pool"
    __table_args__ = {"schema": APP_SCHEMA}

    provider_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.provider.id", nullable=False, index=True
    )
    label: str = Field(max_length=255)
    api_key_encrypted: str = Field(max_length=512)
    weight: int = Field(default=1, nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    last_health_check_at: datetime | None = Field(default=None)
    consecutive_failures: int = Field(default=0, nullable=False)


class ModelProfile(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    """Model configuration profile — platform-level or tenant-scoped."""

    __tablename__ = "model_profile"
    __table_args__ = {"schema": APP_SCHEMA}

    name: str = Field(max_length=255)
    use_case: str = Field(max_length=32)  # chat, embed, rerank
    provider_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.provider.id", nullable=False, index=True
    )
    model_name: str = Field(max_length=255)
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    fallback_profile_id: UUID | None = Field(default=None)
    is_active: bool = Field(default=True, nullable=False)


class TenantModelBinding(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    """Maps a tenant to a model profile for a given use_case."""

    __tablename__ = "tenant_model_binding"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "use_case", name="uq_tenant_binding_use_case"
        ),
        {"schema": APP_SCHEMA},
    )

    use_case: str = Field(max_length=32)  # chat, embed, rerank
    profile_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.model_profile.id", nullable=False, index=True
    )
    is_default: bool = Field(default=False, nullable=False)
