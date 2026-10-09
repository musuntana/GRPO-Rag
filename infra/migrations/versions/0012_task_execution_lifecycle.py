"""Persistent deadline/cancellation and immutable task execution envelopes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_task_execution_lifecycle"
down_revision = "0011_index_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
        schema="app",
    )
    for name in ("deadline_at", "cancel_requested_at", "finished_at"):
        op.add_column("task", sa.Column(name, sa.DateTime(timezone=True)), schema="app")
    op.add_column("task", sa.Column("error_code", sa.String(64)), schema="app")
    op.create_check_constraint(
        "ck_task_timeout_budget", "task", "timeout_seconds BETWEEN 1 AND 3600", schema="app"
    )
    op.create_check_constraint(
        "ck_task_attempts_nonnegative", "task", "attempts >= 0", schema="app"
    )
    op.create_check_constraint(
        "ck_task_lifecycle_state",
        "task",
        "status IN ('pending','retryable','leased','completed','failed','cancelled','timed_out')",
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.reject_task_envelope_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF ROW(NEW.id,NEW.tenant_id,NEW.kind,NEW.payload,
                   NEW.idempotency_key,NEW.timeout_seconds)
            IS DISTINCT FROM
               ROW(OLD.id,OLD.tenant_id,OLD.kind,OLD.payload,OLD.idempotency_key,OLD.timeout_seconds)
            THEN RAISE EXCEPTION 'immutable task envelope cannot be changed' USING ERRCODE='23514';
            END IF;
            IF OLD.deadline_at IS NOT NULL AND NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
            THEN RAISE EXCEPTION 'task total deadline cannot be changed' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute(
        "CREATE TRIGGER immutable_task_envelope BEFORE UPDATE ON app.task "
        "FOR EACH ROW EXECUTE FUNCTION app.reject_task_envelope_rewrite()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security=off")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM app.task WHERE deadline_at IS NOT NULL "
                "OR cancel_requested_at IS NOT NULL OR finished_at IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("task lifecycle downgrade refused: execution facts must be preserved")
    op.execute("DROP TRIGGER immutable_task_envelope ON app.task")
    op.execute("DROP FUNCTION app.reject_task_envelope_rewrite()")
    for constraint in (
        "ck_task_timeout_budget",
        "ck_task_attempts_nonnegative",
        "ck_task_lifecycle_state",
    ):
        op.drop_constraint(constraint, "task", type_="check", schema="app")
    for name in (
        "error_code",
        "finished_at",
        "cancel_requested_at",
        "deadline_at",
        "timeout_seconds",
    ):
        op.drop_column("task", name, schema="app")
