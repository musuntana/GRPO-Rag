"""bootstrap pg scaffold

Revision ID: 0001_pg_scaffold
Revises:
Create Date: 2026-04-11 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_pg_scaffold"
down_revision = None
branch_labels = None
depends_on = None

APP_SCHEMA = "app"
SEARCH_SCHEMA = "search"
CHECKPOINT_SCHEMA = "checkpoint"

SEARCH_VECTOR_SQL = """
setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(headings_text, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(body, '')), 'C') ||
setweight(to_tsvector('simple', coalesce(tags_text, '')), 'D')
""".strip()


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{APP_SCHEMA}"')
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SEARCH_SCHEMA}"')
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')

    op.create_table(
        "tenant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("slug", name="uq_tenant_slug"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_tenant_slug", "tenant", ["slug"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "knowledge_base",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_knowledge_base_tenant_id_tenant"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_knowledge_base_tenant_slug"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_knowledge_base_tenant_id", "knowledge_base", ["tenant_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "document_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=True),
        sa.Column("ingest_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["kb_id"], [f"{APP_SCHEMA}.knowledge_base.id"], name="fk_document_record_kb_id_knowledge_base"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_document_record_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_document_record_tenant_id", "document_record", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_document_record_kb_id", "document_record", ["kb_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "workflow_definition",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_workflow_definition_tenant_id_tenant"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_workflow_definition_tenant_slug"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_workflow_definition_tenant_id", "workflow_definition", ["tenant_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "workflow_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dsl", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["workflow_definition_id"],
            [f"{APP_SCHEMA}.workflow_definition.id"],
            name="fk_workflow_version_workflow_definition_id_workflow_definition",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_workflow_version_tenant_id_tenant"),
        sa.UniqueConstraint("workflow_definition_id", "version", name="uq_workflow_version_number"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_workflow_version_tenant_id", "workflow_version", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_workflow_version_workflow_definition_id", "workflow_version", ["workflow_definition_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "conversation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_conversation_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_conversation_tenant_id", "conversation", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_conversation_external_user_id", "conversation", ["external_user_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "message_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("is_summary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["conversation_id"], [f"{APP_SCHEMA}.conversation.id"], name="fk_message_index_conversation_id_conversation"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_message_index_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_message_index_tenant_id", "message_index", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_message_index_conversation_id", "message_index", ["conversation_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "memory_summary",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["conversation_id"], [f"{APP_SCHEMA}.conversation.id"], name="fk_memory_summary_conversation_id_conversation"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_memory_summary_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_memory_summary_tenant_id", "memory_summary", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_memory_summary_conversation_id", "memory_summary", ["conversation_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "memory_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("decay_score", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["conversation_id"], [f"{APP_SCHEMA}.conversation.id"], name="fk_memory_fact_conversation_id_conversation"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_memory_fact_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_memory_fact_tenant_id", "memory_fact", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_memory_fact_conversation_id", "memory_fact", ["conversation_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "chunk_manifest",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("indexing_state", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["document_id"], [f"{APP_SCHEMA}.document_record.id"], name="fk_chunk_manifest_document_id_document_record"),
        sa.ForeignKeyConstraint(["kb_id"], [f"{APP_SCHEMA}.knowledge_base.id"], name="fk_chunk_manifest_kb_id_knowledge_base"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_chunk_manifest_tenant_id_tenant"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_chunk_manifest_tenant_id", "chunk_manifest", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_chunk_manifest_kb_id", "chunk_manifest", ["kb_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_chunk_manifest_document_id", "chunk_manifest", ["document_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_run_tenant_id_tenant"),
        sa.ForeignKeyConstraint(["workflow_version_id"], [f"{APP_SCHEMA}.workflow_version.id"], name="fk_run_workflow_version_id_workflow_version"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_run_tenant_id", "run", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_run_workflow_version_id", "run", ["workflow_version_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_run_thread_id", "run", ["thread_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "run_summary",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["run_id"], [f"{APP_SCHEMA}.run.id"], name="fk_run_summary_run_id_run"),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_run_summary_tenant_id_tenant"),
        sa.UniqueConstraint("run_id", name="uq_run_summary_run_id"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_run_summary_tenant_id", "run_summary", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_run_summary_run_id", "run_summary", ["run_id"], unique=False, schema=APP_SCHEMA)

    op.create_table(
        "task",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_task_tenant_id_tenant"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_tenant_idempotency_key"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_task_tenant_id", "task", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_task_status", "task", ["status"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_task_available_at", "task", ["available_at"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_task_lease_until", "task", ["lease_until"], unique=False, schema=APP_SCHEMA)

    for table_name in ("chunk_stable", "chunk_hot", "memory"):
        op.create_table(
            table_name,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("parent_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("layer", sa.String(length=16), nullable=False, server_default=sa.text("'L1'")),
            sa.Column("lang", sa.String(length=16), nullable=False, server_default=sa.text("'simple'")),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("headings_text", sa.Text(), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("tags_text", sa.Text(), nullable=True),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "search_vector",
                postgresql.TSVECTOR(),
                sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
                nullable=False,
            ),
            schema=SEARCH_SCHEMA,
        )
        op.create_index(
            f"ix_{table_name}_search_vector",
            table_name,
            ["search_vector"],
            unique=False,
            schema=SEARCH_SCHEMA,
            postgresql_using="gin",
        )

    op.create_index(
        "ix_chunk_stable_scope",
        "chunk_stable",
        ["tenant_id", "kb_id", "layer", "updated_at"],
        unique=False,
        schema=SEARCH_SCHEMA,
    )
    op.create_index(
        "ix_chunk_hot_scope",
        "chunk_hot",
        ["tenant_id", "kb_id", "layer", "updated_at"],
        unique=False,
        schema=SEARCH_SCHEMA,
    )
    op.create_index(
        "ix_memory_scope",
        "memory",
        ["tenant_id", "updated_at"],
        unique=False,
        schema=SEARCH_SCHEMA,
    )


def downgrade() -> None:
    for table_name in ("memory", "chunk_hot", "chunk_stable"):
        op.drop_table(table_name, schema=SEARCH_SCHEMA)

    for table_name in (
        "task",
        "run_summary",
        "run",
        "chunk_manifest",
        "memory_fact",
        "memory_summary",
        "message_index",
        "conversation",
        "workflow_version",
        "workflow_definition",
        "document_record",
        "knowledge_base",
        "tenant",
    ):
        op.drop_table(table_name, schema=APP_SCHEMA)

    op.execute(f'DROP SCHEMA IF EXISTS "{CHECKPOINT_SCHEMA}" CASCADE')
    op.execute(f'DROP SCHEMA IF EXISTS "{SEARCH_SCHEMA}" CASCADE')
    op.execute(f'DROP SCHEMA IF EXISTS "{APP_SCHEMA}" CASCADE')
