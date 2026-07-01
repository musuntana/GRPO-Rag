"""add provider, key_pool, model_profile, tenant_model_binding tables

Revision ID: 0004_model_gateway
Revises: 0003_user_and_tenant_extras
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004_model_gateway"
down_revision = "0003_user_and_tenant_extras"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"


def upgrade() -> None:
    # 1. Provider table (platform-level, not tenant-scoped)
    op.create_table(
        "provider",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "config",
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
        sa.UniqueConstraint("slug", name="uq_provider_slug"),
        schema=APP_SCHEMA,
    )

    # 2. KeyPool table
    op.create_table(
        "key_pool",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("api_key_encrypted", sa.String(length=512), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
            ["provider_id"],
            [f"{APP_SCHEMA}.provider.id"],
            name="fk_key_pool_provider_id_provider",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_key_pool_provider_id", "key_pool", ["provider_id"], unique=False, schema=APP_SCHEMA)

    # 3. ModelProfile table (tenant-scoped)
    op.create_table(
        "model_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("use_case", sa.String(length=32), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("fallback_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            ["provider_id"],
            [f"{APP_SCHEMA}.provider.id"],
            name="fk_model_profile_provider_id_provider",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{APP_SCHEMA}.tenant.id"],
            name="fk_model_profile_tenant_id_tenant",
        ),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_model_profile_tenant_id", "model_profile", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_model_profile_provider_id", "model_profile", ["provider_id"], unique=False, schema=APP_SCHEMA)

    # 4. TenantModelBinding table (tenant-scoped)
    op.create_table(
        "tenant_model_binding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("use_case", sa.String(length=32), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
            ["profile_id"],
            [f"{APP_SCHEMA}.model_profile.id"],
            name="fk_tenant_model_binding_profile_id_model_profile",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{APP_SCHEMA}.tenant.id"],
            name="fk_tenant_model_binding_tenant_id_tenant",
        ),
        sa.UniqueConstraint("tenant_id", "use_case", name="uq_tenant_binding_use_case"),
        schema=APP_SCHEMA,
    )
    op.create_index("ix_tenant_model_binding_tenant_id", "tenant_model_binding", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_tenant_model_binding_profile_id", "tenant_model_binding", ["profile_id"], unique=False, schema=APP_SCHEMA)

    # 5. Enable RLS on the new tenant-scoped tables
    rls_tables = ("model_profile", "tenant_model_binding")
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
    for table_name in ("tenant_model_binding", "model_profile"):
        op.execute(f'ALTER TABLE {APP_SCHEMA}."{table_name}" DISABLE ROW LEVEL SECURITY')
    op.drop_table("tenant_model_binding", schema=APP_SCHEMA)
    op.drop_table("model_profile", schema=APP_SCHEMA)
    op.drop_table("key_pool", schema=APP_SCHEMA)
    op.drop_table("provider", schema=APP_SCHEMA)
