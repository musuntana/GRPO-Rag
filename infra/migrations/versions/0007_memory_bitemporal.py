"""bi-temporal memory facts, summary windows, and memory profile versions

Revision ID: 0007_memory_bitemporal
Revises: 0006_retriever_profile_gate
Create Date: 2026-10-04

app.memory_fact additions
    user_id, subject_key, valid_from, valid_until, ingested_at,
    superseded_by, mention_count.

A fact is a statement about the world at a time, not a mutable preference row.
The prior text is kept and closed with ``valid_until`` rather than overwritten,
so "what did we agree on last month" has an answer and a contradicting fact does
not erase the evidence that it replaced something.

``subject_key`` is the slot a fact occupies ("language_preference",
"billing_contact"). Contradiction is detected per slot, which is why it cannot be
inferred from free text: two facts about the same subject are only comparable
once something has named the subject.

app.memory_summary additions
    turn_range_start, turn_range_end, summary_version.

The window a summary covers is recorded, so re-running a summarization over the
same boundary is idempotent and the conversation buffer knows which turns are
already compressed.

app additions
    memory_profile_version.

The summary window ``N`` is an evaluated parameter, not a default. This table is
where the value and the eval run that justified it live; a code path that
summarizes without a recorded version has an unfalsifiable heuristic.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_memory_bitemporal"
down_revision = "0006_retriever_profile_gate"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)

FACT_COLUMNS: tuple[sa.Column, ...] = (
    # Actor attribution: per-user export and DSAR deletion are not expressible
    # without a user dimension on the row.
    sa.Column("user_id", _UUID, nullable=True),
    # The slot this fact occupies. Empty means "no slot", which never contradicts.
    sa.Column(
        "subject_key",
        sa.String(length=128),
        nullable=False,
        server_default=sa.text("''"),
    ),
    sa.Column(
        "valid_from",
        _TS,
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    # NULL means "still true". Setting it is how a fact is superseded.
    sa.Column("valid_until", _TS, nullable=True),
    # When the system learned it, which is not when it became true.
    sa.Column(
        "ingested_at",
        _TS,
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column("superseded_by", _UUID, nullable=True),
    # Mention frequency is the first, cheapest signal on the memory rerank ladder.
    sa.Column("mention_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
)

SUMMARY_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("turn_range_start", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("turn_range_end", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("summary_version", sa.String(length=64), nullable=True),
)


def upgrade() -> None:
    for column in FACT_COLUMNS:
        op.add_column("memory_fact", column, schema=APP_SCHEMA)
    for column in SUMMARY_COLUMNS:
        op.add_column("memory_summary", column, schema=APP_SCHEMA)

    # Active-fact reads filter on (tenant, user, valid_until IS NULL) and the
    # decay sweep sorts on decay_score; both are the hot paths for every chat
    # turn, so they are indexes rather than a scan.
    op.create_index(
        "ix_memory_fact_active",
        "memory_fact",
        ["tenant_id", "user_id", "valid_until"],
        unique=False,
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_memory_fact_decay",
        "memory_fact",
        ["tenant_id", "decay_score"],
        unique=False,
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_memory_fact_subject",
        "memory_fact",
        ["tenant_id", "user_id", "subject_key"],
        unique=False,
        schema=APP_SCHEMA,
    )

    op.create_table(
        "memory_profile_version",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # The evaluated parameter. No default anywhere in the code path.
        sa.Column("window_n", sa.Integer(), nullable=False),
        sa.Column(
            "strategy",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'rolling'"),
        ),
        sa.Column("summary_prompt_version", sa.String(length=64), nullable=True),
        # The eval run that produced window_n. Nullable so a profile can be
        # recorded as "not yet evaluated", which is a state worth distinguishing
        # from "evaluated and fine".
        sa.Column("eval_run_id", _UUID, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_memory_profile_version_tenant_id"
        ),
        sa.UniqueConstraint("tenant_id", "version", name="uq_memory_profile_version"),
        schema=APP_SCHEMA,
    )
    op.create_index(
        "ix_memory_profile_version_tenant_id",
        "memory_profile_version",
        ["tenant_id"],
        unique=False,
        schema=APP_SCHEMA,
    )

    op.execute(
        f'ALTER TABLE {APP_SCHEMA}."memory_profile_version" ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_memory_profile_version "
        f'ON {APP_SCHEMA}."memory_profile_version" '
        f"FOR ALL "
        f"USING ({APP_SCHEMA}.tenant_filter_check()) "
        f"WITH CHECK ({APP_SCHEMA}.tenant_filter_check())"
    )


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS tenant_isolation_memory_profile_version '
        f'ON {APP_SCHEMA}."memory_profile_version"'
    )
    op.drop_table("memory_profile_version", schema=APP_SCHEMA)

    op.drop_index("ix_memory_fact_subject", table_name="memory_fact", schema=APP_SCHEMA)
    op.drop_index("ix_memory_fact_decay", table_name="memory_fact", schema=APP_SCHEMA)
    op.drop_index("ix_memory_fact_active", table_name="memory_fact", schema=APP_SCHEMA)

    for column in reversed(SUMMARY_COLUMNS):
        op.drop_column("memory_summary", column.name, schema=APP_SCHEMA)
    for column in reversed(FACT_COLUMNS):
        op.drop_column("memory_fact", column.name, schema=APP_SCHEMA)
