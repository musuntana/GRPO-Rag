"""Durable fail-closed chat request idempotency claims.

Revision ID: 0013_chat_request_claims
Revises: 0012_task_execution_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_chat_request_claims"
down_revision = "0012_task_execution_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    u = postgresql.UUID(as_uuid=True)
    op.create_table(
        "chat_request",
        sa.Column("id", u, primary_key=True, nullable=False),
        sa.Column("tenant_id", u, nullable=False),
        sa.Column("user_id", u, nullable=False),
        sa.Column("conversation_id", u, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="claimed"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["app.tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app.user.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["app.conversation.id"]),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "conversation_id", "idempotency_key",
            name="uq_chat_request_actor_key",
        ),
        sa.CheckConstraint(
            "status IN ('claimed','completed','failed')",
            name="ck_chat_request_status",
        ),
        schema="app",
    )
    for col in ("tenant_id", "user_id", "conversation_id"):
        op.create_index(
            f"ix_chat_request_{col}", "chat_request", [col], schema="app"
        )
    op.execute('ALTER TABLE app."chat_request" ENABLE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY tenant_isolation_chat_request ON app."chat_request" '
        "FOR ALL USING (app.tenant_filter_check(tenant_id)) "
        "WITH CHECK (app.tenant_filter_check(tenant_id))"
    )
    # Reject mutation of claimed actor, source request or its fingerprint.
    op.execute("""
        CREATE FUNCTION app.reject_chat_request_identity_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            IF ROW(NEW.id,NEW.tenant_id,NEW.user_id,NEW.conversation_id,
                   NEW.idempotency_key,NEW.request_digest,NEW.created_at)
            IS DISTINCT FROM
               ROW(OLD.id,OLD.tenant_id,OLD.user_id,OLD.conversation_id,
                   OLD.idempotency_key,OLD.request_digest,OLD.created_at)
            THEN
                RAISE EXCEPTION 'immutable chat request identity'
                    USING ERRCODE='23514';
            END IF;
            IF OLD.status <> 'claimed' AND NEW.status IS DISTINCT FROM OLD.status
            THEN
                RAISE EXCEPTION 'terminal chat request cannot transition'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
    """)
    op.execute(
        "CREATE TRIGGER immutable_chat_request_identity "
        "BEFORE UPDATE ON app.chat_request "
        "FOR EACH ROW EXECUTE FUNCTION app.reject_chat_request_identity_rewrite()"
    )


def downgrade() -> None:
    # Refuse to destroy charged/committed request identities or ambiguous
    # in-flight claims. A destructive rollback needs a separate audit.
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM app.chat_request LIMIT 1)")
    ).scalar():
        raise RuntimeError(
            "chat request downgrade refused: preserve idempotency and billing evidence"
        )
    op.execute("DROP TRIGGER immutable_chat_request_identity ON app.chat_request")
    op.execute("DROP FUNCTION app.reject_chat_request_identity_rewrite()")
    op.execute('DROP POLICY tenant_isolation_chat_request ON app."chat_request"')
    op.drop_table("chat_request", schema="app")
