"""add canonical tenant policy fields

Revision ID: 0002_tenant_policy_fields
Revises: 0001_pg_scaffold
Create Date: 2026-04-11 00:00:01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_tenant_policy_fields"
down_revision = "0001_pg_scaffold"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column(
            "plan_tier",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'standard'"),
        ),
        schema=APP_SCHEMA,
    )
    op.add_column(
        "tenant",
        sa.Column(
            "locale",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'zh-CN'"),
        ),
        schema=APP_SCHEMA,
    )
    op.add_column(
        "tenant",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'Asia/Shanghai'"),
        ),
        schema=APP_SCHEMA,
    )
    op.add_column(
        "tenant",
        sa.Column(
            "security_level",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        schema=APP_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("tenant", "security_level", schema=APP_SCHEMA)
    op.drop_column("tenant", "timezone", schema=APP_SCHEMA)
    op.drop_column("tenant", "locale", schema=APP_SCHEMA)
    op.drop_column("tenant", "plan_tier", schema=APP_SCHEMA)
