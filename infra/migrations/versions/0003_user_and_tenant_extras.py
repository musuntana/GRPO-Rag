"""add user, tenant_domain, tenant_brand tables and enable RLS

Revision ID: 0003_user_and_tenant_extras
Revises: 0002_tenant_policy_fields
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003_user_and_tenant_extras"
down_revision = "0002_tenant_policy_fields"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"


def upgrade() -> None:
    # 1. User table
    op.create_table(
        "user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column(
            "roles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{APP_SCHEMA}.tenant.id"],
            name="fk_user_tenant_id_tenant",
        ),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_user_tenant_external"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_user_tenant_id", "user", ["tenant_id"], unique=False, schema=APP_SCHEMA)

    # 2. TenantDomain table
    op.create_table(
        "tenant_domain",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{APP_SCHEMA}.tenant.id"],
            name="fk_tenant_domain_tenant_id_tenant",
        ),
        sa.UniqueConstraint("domain", name="uq_tenant_domain_domain"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_tenant_domain_tenant_id",
        "tenant_domain",
        ["tenant_id"],
        unique=False,
        schema=APP_SCHEMA,
    )

    # 3. TenantBrand table
    op.create_table(
        "tenant_brand",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("brand_name", sa.String(length=255), nullable=False),
        sa.Column("logo_url", sa.String(length=1024), nullable=True),
        sa.Column("primary_color", sa.String(length=7), nullable=True),
        sa.Column(
            "css_variables",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{APP_SCHEMA}.tenant.id"],
            name="fk_tenant_brand_tenant_id_tenant",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_tenant_brand_tenant_id",
        "tenant_brand",
        ["tenant_id"],
        unique=False,
        schema=APP_SCHEMA,
    )

    # 4. Enable Row-Level Security on all tenant-scoped app schema tables.
    rls_function = f"""
    CREATE OR REPLACE FUNCTION {APP_SCHEMA}.tenant_filter_check()
    RETURNS boolean AS $$
    DECLARE
        current_tid text;
    BEGIN
        current_tid := current_setting('{APP_SCHEMA}.current_tenant_id', true);
        IF current_tid IS NULL OR current_tid = '' THEN
            RETURN true;
        END IF;
        RETURN tenant_id = current_tid::uuid;
    EXCEPTION WHEN OTHERS THEN
        RETURN true;
    END;
    $$ LANGUAGE plpgsql STABLE SECURITY DEFINER;
    """
    op.execute(rls_function)

    rls_tables = (
        "knowledge_base",
        "document_record",
        "chunk_manifest",
        "workflow_definition",
        "workflow_version",
        "conversation",
        "message_index",
        "memory_summary",
        "memory_fact",
        "run",
        "run_summary",
        "task",
        "user",
        "tenant_domain",
        "tenant_brand",
    )
    for table_name in rls_tables:
        op.execute(f'ALTER TABLE {APP_SCHEMA}."{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f"CREATE POLICY tenant_isolation_{table_name} "
            f'ON {APP_SCHEMA}."{table_name}" '
            f"FOR ALL "
            f"USING ({APP_SCHEMA}.tenant_filter_check()) "
            f"WITH CHECK ({APP_SCHEMA}.tenant_filter_check())"
        )


def downgrade() -> None:
    rls_tables = (
        "tenant_brand",
        "tenant_domain",
        "user",
        "knowledge_base",
        "document_record",
        "chunk_manifest",
        "workflow_definition",
        "workflow_version",
        "conversation",
        "message_index",
        "memory_summary",
        "memory_fact",
        "run",
        "run_summary",
        "task",
    )
    for table_name in rls_tables:
        op.execute(f'ALTER TABLE {APP_SCHEMA}."{table_name}" DISABLE ROW LEVEL SECURITY')

    op.execute(f"DROP FUNCTION IF EXISTS {APP_SCHEMA}.tenant_filter_check()")

    op.drop_table("tenant_brand", schema=APP_SCHEMA)
    op.drop_table("tenant_domain", schema=APP_SCHEMA)
    op.drop_table("user", schema=APP_SCHEMA)
