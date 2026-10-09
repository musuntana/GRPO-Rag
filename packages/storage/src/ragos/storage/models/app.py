from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from ragos.storage.models.base import RagosModel, Timestamped, UUIDPrimaryKey, utc_now
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
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
    description: str | None = Field(sa_type=Text, default=None)


class DocumentRecord(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "document_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kb_id", "id", name="uq_document_owner_id"),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id", "id", "active_index_id"],
            [
                "app.chunk_manifest.tenant_id",
                "app.chunk_manifest.kb_id",
                "app.chunk_manifest.document_id",
                "app.chunk_manifest.id",
            ],
            name="fk_document_active_index_owner",
            use_alter=True,
        ),
        {"schema": APP_SCHEMA},
    )
    active_index_id: UUID | None = Field(default=None)

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
    description: str | None = Field(sa_type=Text, default=None)


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
    content: str = Field(sa_type=Text, nullable=False)
    token_count: int | None = Field(default=None)
    is_summary: bool = Field(default=False, nullable=False)
    run_id: UUID | None = Field(default=None, index=True)
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )


class MemorySummary(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "memory_summary"
    __table_args__ = {"schema": APP_SCHEMA}

    conversation_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.conversation.id",
        nullable=False,
        index=True,
    )
    summary: str = Field(sa_type=Text, nullable=False)
    turn_range_start: int = Field(default=0, nullable=False)
    turn_range_end: int = Field(default=0, nullable=False)
    summary_version: str | None = Field(default=None, max_length=64)


class MemoryFact(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "memory_fact"
    __table_args__ = (
        Index("ix_memory_fact_active", "tenant_id", "user_id", "valid_until"),
        Index("ix_memory_fact_decay", "tenant_id", "decay_score"),
        Index("ix_memory_fact_subject", "tenant_id", "user_id", "subject_key"),
        {"schema": APP_SCHEMA},
    )

    conversation_id: UUID | None = Field(
        default=None,
        foreign_key=f"{APP_SCHEMA}.conversation.id",
        index=True,
    )
    user_id: UUID | None = Field(default=None)
    subject_key: str = Field(default="", max_length=128, nullable=False)
    valid_from: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
        ),
    )
    valid_until: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    ingested_at: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
        ),
    )
    superseded_by: UUID | None = Field(default=None)
    mention_count: int = Field(default=1, nullable=False)
    category: str = Field(max_length=64)
    fact: str = Field(sa_type=Text, nullable=False)
    confidence: float = Field(default=0.5, nullable=False)
    decay_score: float = Field(default=1.0, nullable=False)
    last_accessed_at: datetime | None = Field(sa_type=DateTime(timezone=True), default=None)


class ChunkManifest(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "chunk_manifest"
    __table_args__ = (
        CheckConstraint(
            "indexing_state <> 'indexed' OR (pg_digest IS NOT NULL AND "
            "vector_digest IS NOT NULL AND vector_digest = pg_digest "
            "AND reconciled_at IS NOT NULL)",
            name="ck_manifest_readback_publication",
        ),
        UniqueConstraint("tenant_id", "kb_id", "document_id", "id", name="uq_manifest_owner_id"),
        UniqueConstraint(
            "tenant_id",
            "kb_id",
            "document_id",
            "document_version_id",
            "representation_id",
            "id",
            name="uq_manifest_lineage_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id", "document_id", "document_version_id", "representation_id"],
            [
                "app.document_representation.tenant_id",
                "app.document_representation.kb_id",
                "app.document_representation.document_id",
                "app.document_representation.document_version_id",
                "app.document_representation.id",
            ],
            name="fk_manifest_representation_owner",
        ),
        {"schema": APP_SCHEMA},
    )
    document_version_id: UUID | None = Field(default=None)
    representation_id: UUID | None = Field(default=None)
    chunker_config_digest: str | None = Field(default=None, max_length=64)
    embedding_version: str | None = Field(default=None, max_length=128)
    plan_ref: str | None = Field(default=None, max_length=1024)
    plan_digest: str | None = Field(default=None, max_length=64)
    legacy_indexing_state: str | None = Field(default=None, max_length=32)
    pg_digest: str | None = Field(default=None, max_length=64)
    vector_digest: str | None = Field(default=None, max_length=64)
    reconciled_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))

    kb_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.knowledge_base.id", nullable=False, index=True)
    document_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.document_record.id",
        nullable=False,
        index=True,
    )
    parent_unit: str = Field(default="token_window", max_length=16, nullable=False)
    page_count: int | None = Field(default=None)
    parser_version: str | None = Field(default=None, max_length=128)
    chunker_version: str | None = Field(default=None, max_length=64)
    embedding_profile: str | None = Field(default=None, max_length=64)
    chunk_total: int = Field(default=0, nullable=False)
    indexing_state: str = Field(default="pending", max_length=32)
    error_message: str | None = Field(sa_type=Text, default=None)


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
        CheckConstraint("timeout_seconds BETWEEN 1 AND 3600", name="ck_task_timeout_budget"),
        CheckConstraint("attempts >= 0", name="ck_task_attempts_nonnegative"),
        CheckConstraint(
            "status IN ('pending','retryable','leased','completed','failed',"
            "'cancelled','timed_out')",
            name="ck_task_lifecycle_state",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_tenant_idempotency_key"),
        {"schema": APP_SCHEMA},
    )

    kind: str = Field(max_length=64)
    status: str = Field(default="pending", max_length=32, index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    available_at: datetime | None = Field(sa_type=DateTime(timezone=True), default=None, index=True)
    lease_until: datetime | None = Field(sa_type=DateTime(timezone=True), default=None, index=True)
    attempts: int = Field(default=0, nullable=False)
    timeout_seconds: int = Field(default=300, nullable=False)
    deadline_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    cancel_requested_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    error_code: str | None = Field(default=None, max_length=64)
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

    provider_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.provider.id", nullable=False, index=True)
    label: str = Field(max_length=255)
    api_key_encrypted: str = Field(max_length=512)
    weight: int = Field(default=1, nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    last_health_check_at: datetime | None = Field(sa_type=DateTime(timezone=True), default=None)
    consecutive_failures: int = Field(default=0, nullable=False)


class ModelProfile(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    """Model configuration profile — platform-level or tenant-scoped."""

    __tablename__ = "model_profile"
    __table_args__ = {"schema": APP_SCHEMA}

    name: str = Field(max_length=255)
    use_case: str = Field(max_length=32)  # chat, embed, rerank
    provider_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.provider.id", nullable=False, index=True)
    model_name: str = Field(max_length=255)
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    artifact_ref: str | None = Field(default=None, max_length=1024)
    fallback_profile_id: UUID | None = Field(default=None)
    is_active: bool = Field(default=True, nullable=False)


class TenantModelBinding(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    """Maps a tenant to a model profile for a given use_case."""

    __tablename__ = "tenant_model_binding"
    __table_args__ = (
        UniqueConstraint("tenant_id", "use_case", name="uq_tenant_binding_use_case"),
        {"schema": APP_SCHEMA},
    )

    use_case: str = Field(max_length=32)  # chat, embed, rerank
    profile_id: UUID = Field(
        foreign_key=f"{APP_SCHEMA}.model_profile.id", nullable=False, index=True
    )
    is_default: bool = Field(default=False, nullable=False)


# These mappings describe existing migrations, not completed training/evaluation.
def json_object_field():
    return Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


def json_list_field():
    return Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )


class RetrieverProfileVersion(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "retriever_profile_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "profile_id", "version", name="uq_retriever_profile_version"),
        Index("ix_rpv_tenant_id", "tenant_id"),
        Index("ix_rpv_profile_id", "profile_id"),
        {"schema": APP_SCHEMA},
    )
    tenant_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.tenant.id", nullable=False)
    profile_id: UUID = Field(nullable=False)
    version: int = Field(default=1, nullable=False)
    name: str | None = Field(default=None, max_length=128)
    query_strategy: str = Field(default="direct", max_length=32, nullable=False)
    recall_channels: list[str] = json_list_field()
    join_mode: str = Field(default="reciprocal_rank_fusion", max_length=32, nullable=False)
    channel_budgets: dict[str, Any] = json_object_field()
    fusion_config: dict[str, Any] = json_object_field()
    crag_thresholds: dict[str, Any] = json_object_field()
    crag_enabled: bool = Field(default=True, nullable=False)
    rerank_model_profile: str | None = Field(default=None, max_length=128)
    citation_verify_semantic: bool = Field(default=False, nullable=False)
    top_k_recall: int = Field(default=50, nullable=False)
    top_k_rerank: int = Field(default=20, nullable=False)
    top_k_generate: int = Field(default=6, nullable=False)


class EvalCase(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "eval_case"
    __table_args__ = (Index("ix_eval_case_split", "tenant_id", "split"), {"schema": APP_SCHEMA})
    kb_id: UUID | None = Field(default=None, index=True)
    question: str = Field(sa_column=Column(Text, nullable=False))
    conversation_turns: list[dict[str, Any]] = json_list_field()
    gold_evidence: list[dict[str, Any]] = json_list_field()
    expected_answer: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    answerable: bool = Field(default=True, nullable=False)
    supported_claims: list[str] = json_list_field()
    review_state: str = Field(default="pending", max_length=16, nullable=False)
    labeler: str | None = Field(default=None, max_length=255)
    split: str = Field(default="dev", max_length=16, nullable=False)


class EvalRun(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "eval_run"
    __table_args__ = {"schema": APP_SCHEMA}
    bundle_id: str = Field(max_length=128, nullable=False, index=True)
    bundle: dict[str, Any] = json_object_field()
    metrics: dict[str, Any] = json_object_field()
    case_total: int = Field(default=0, nullable=False)
    split: str = Field(default="holdout", max_length=16, nullable=False)
    status: str = Field(default="completed", max_length=32, nullable=False)


class GraderValidation(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "grader_validation"
    __table_args__ = (
        Index("ix_gv_version", "rails_prompt_version"),
        Index("ix_gv_tenant_id", "tenant_id"),
        {"schema": APP_SCHEMA},
    )
    tenant_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.tenant.id", nullable=False)
    rails_prompt_version: str = Field(max_length=64, nullable=False)
    sample_size: int = Field(default=0, nullable=False)
    agreement: float = Field(default=0.0, nullable=False)
    confusion: dict[str, Any] = json_object_field()


class RerankerArtifact(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "reranker_artifact"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_reranker_artifact_tenant_version"),
        {"schema": APP_SCHEMA},
    )
    version: int = Field(default=1, nullable=False)
    method: str = Field(default="sft", max_length=32, nullable=False)
    scope_key: str = Field(max_length=255, nullable=False)
    artifact_ref: str = Field(max_length=1024, nullable=False)
    source_bundle: dict[str, Any] = json_object_field()
    eval_metrics: dict[str, Any] = json_object_field()
    is_promoted: bool = Field(default=False, nullable=False, index=True)


class MemoryProfileVersion(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "memory_profile_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_memory_profile_version"),
        {"schema": APP_SCHEMA},
    )
    version: int = Field(default=1, nullable=False)
    window_n: int = Field(nullable=False)
    strategy: str = Field(default="rolling", max_length=32, nullable=False)
    summary_prompt_version: str | None = Field(default=None, max_length=64)
    eval_run_id: UUID | None = Field(default=None)
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class FeedbackEntry(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "feedback_entry"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_feedback_entry_message_user"),
        Index("ix_feedback_entry_rating", "tenant_id", "rating"),
        {"schema": APP_SCHEMA},
    )
    conversation_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.conversation.id", nullable=False)
    message_id: UUID = Field(foreign_key=f"{APP_SCHEMA}.message_index.id", nullable=False)
    user_id: UUID | None = Field(default=None)
    run_id: UUID | None = Field(default=None)
    rating: str = Field(max_length=16, nullable=False)
    comment: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    answer_snapshot: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class DocumentVersion(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kb_id", "document_id", "id", name="uq_document_version_owner_id"
        ),
        UniqueConstraint(
            "tenant_id", "document_id", "source_digest", name="uq_document_version_digest"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id", "document_id"],
            [
                "app.document_record.tenant_id",
                "app.document_record.kb_id",
                "app.document_record.id",
            ],
            name="fk_document_version_owner",
        ),
        {"schema": APP_SCHEMA},
    )
    kb_id: UUID
    document_id: UUID
    source_digest: str = Field(max_length=64)
    source_ref: str = Field(max_length=1024)
    media_type: str = Field(max_length=128)


class DocumentRepresentation(UUIDPrimaryKey, AppTenantScoped, Timestamped, RagosModel, table=True):
    __tablename__ = "document_representation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kb_id",
            "document_id",
            "document_version_id",
            "id",
            name="uq_representation_owner_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "representation_digest",
            "parser_version",
            "render_version",
            name="uq_representation_content_profile",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id", "document_id", "document_version_id"],
            [
                "app.document_version.tenant_id",
                "app.document_version.kb_id",
                "app.document_version.document_id",
                "app.document_version.id",
            ],
            name="fk_representation_version_owner",
        ),
        {"schema": APP_SCHEMA},
    )
    kb_id: UUID
    document_id: UUID
    document_version_id: UUID
    representation_digest: str = Field(max_length=64)
    representation_ref: str = Field(max_length=1024)
    parser_version: str = Field(max_length=128)
    render_version: str = Field(max_length=128)
    parent_unit: str = Field(max_length=16)
    char_count: int
    page_count: int | None = Field(default=None)
