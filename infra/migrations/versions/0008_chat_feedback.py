"""chat and feedback storage

Revision ID: 0008_chat_feedback
Revises: 0007_memory_bitemporal
Create Date: 2026-10-04

app.message_index additions
    run_id, citations.

The assistant message and its citations are stored together on one row. A citation
that lives in a separate table, or only in the run trace, cannot be read next to
the sentence it supports — and a citation the user clicks has to survive the run
that produced it.

app additions
    feedback_entry.

Thumbs up/down is an input to the eval dataset, not a UI preference: it is the
only place a user's judgement of an answer is recorded. It carries the message, the
actor, and the run so a bad answer can be traced back to the configuration that
produced it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_chat_feedback"
down_revision = "0007_memory_bitemporal"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)

MESSAGE_COLUMNS: tuple[sa.Column, ...] = (
    # Ties the message to the run whose trace explains how it was produced.
    sa.Column("run_id", _UUID, nullable=True),
    sa.Column(
        "citations",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
)

#: ``up`` / ``down``. A neutral rating is a distinct state from an absent one:
#: "asked, no opinion" and "never asked" are different facts about an answer.
RATINGS = ("up", "down")


def upgrade() -> None:
    for column in MESSAGE_COLUMNS:
        op.add_column("message_index", column, schema=APP_SCHEMA)
    op.create_index(
        "ix_message_index_run_id",
        "message_index",
        ["run_id"],
        unique=False,
        schema=APP_SCHEMA,
    )

    op.create_table(
        "feedback_entry",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("message_id", _UUID, nullable=False),
        # Who rated it. A tenant admin looking at a transcript is not the user.
        sa.Column("user_id", _UUID, nullable=True),
        sa.Column("run_id", _UUID, nullable=True),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        # What the answer looked like when it was rated. A feedback row whose
        # subject has since changed is feedback about nothing.
        sa.Column("answer_snapshot", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_feedback_entry_tenant_id"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{APP_SCHEMA}.conversation.id"],
            name="fk_feedback_entry_conversation_id",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            [f"{APP_SCHEMA}.message_index.id"],
            name="fk_feedback_entry_message_id",
        ),
        # One rating per actor per message. A re-rating updates the row rather than
        # accumulating contradictory rows.
        sa.UniqueConstraint(
            "message_id", "user_id", name="uq_feedback_entry_message_user"
        ),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_feedback_entry_tenant_id",
        "feedback_entry",
        ["tenant_id"],
        unique=False,
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_feedback_entry_rating",
        "feedback_entry",
        ["tenant_id", "rating"],
        unique=False,
        schema=APP_SCHEMA,
    )
    op.execute(f'ALTER TABLE {APP_SCHEMA}."feedback_entry" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f"CREATE POLICY tenant_isolation_feedback_entry "
        f'ON {APP_SCHEMA}."feedback_entry" '
        f"FOR ALL "
        f"USING ({APP_SCHEMA}.tenant_filter_check()) "
        f"WITH CHECK ({APP_SCHEMA}.tenant_filter_check())"
    )


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS tenant_isolation_feedback_entry '
        f'ON {APP_SCHEMA}."feedback_entry"'
    )
    op.drop_table("feedback_entry", schema=APP_SCHEMA)
    op.drop_index("ix_message_index_run_id", table_name="message_index", schema=APP_SCHEMA)
    for column in reversed(MESSAGE_COLUMNS):
        op.drop_column("message_index", column.name, schema=APP_SCHEMA)
