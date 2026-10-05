"""retriever profile gate flag and display name

Revision ID: 0006_retriever_profile_gate
Revises: 0005_page_anchored_eval
Create Date: 2026-10-04

app.retriever_profile_version additions
    crag_enabled, name.

``crag_enabled`` exists so the relevance gate can be turned off per profile.
A gate that cannot be switched off cannot be A/B'd, and thresholds that cannot
be compared against a no-gate run are unfalsifiable — the guard against
hallucinated answers then quietly becomes the reason answers get refused.

``name`` is a label for the profile list. ``profile_id`` is the identity; the
name is presentation only and carries no behaviour.

Both are additive with defaults, so existing versioned rows keep meaning the
same thing: their gate was on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_retriever_profile_gate"
down_revision = "0005_page_anchored_eval"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"

TABLE = "retriever_profile_version"

#: Column definitions are shared with the model-consistency test, which reads
#: them off this module rather than restating them.
NEW_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("name", sa.String(length=128), nullable=True),
    # Existing rows had a gate; there is no evidence they did not want one.
    sa.Column("crag_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
)


def upgrade() -> None:
    for column in NEW_COLUMNS:
        op.add_column(TABLE, column, schema=APP_SCHEMA)
    # Rows written before this revision carry NULL in the new boolean column on
    # databases where the default was applied as a backfill rather than inline.
    op.execute(
        f'UPDATE {APP_SCHEMA}."{TABLE}" SET "crag_enabled" = true WHERE "crag_enabled" IS NULL'
    )


def downgrade() -> None:
    for column in reversed(NEW_COLUMNS):
        op.drop_column(TABLE, column.name, schema=APP_SCHEMA)
