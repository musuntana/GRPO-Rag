"""Bind each RLS policy to the actual row tenant and refuse missing scope."""

from __future__ import annotations

from alembic import op

revision = "0009_fail_closed_rls"
down_revision = "0008_chat_feedback"
branch_labels = None
depends_on = None

APP_TABLES = (
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
    "model_profile",
    "tenant_model_binding",
    "retriever_profile_version",
    "eval_case",
    "eval_run",
    "grader_validation",
    "reranker_artifact",
    "memory_profile_version",
    "feedback_entry",
)
SEARCH_TABLES = ("chunk_stable", "chunk_hot", "memory")


def _policies(predicate: str) -> None:
    for schema, tables in (("app", APP_TABLES), ("search", SEARCH_TABLES)):
        for table in tables:
            op.execute(f'ALTER TABLE {schema}."{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'DROP POLICY IF EXISTS tenant_isolation_{table} ON {schema}."{table}"')
            op.execute(
                f'CREATE POLICY tenant_isolation_{table} ON {schema}."{table}" '
                f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
            )


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION app.tenant_filter_check(row_tenant uuid)
        RETURNS boolean LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path = pg_catalog AS $$
        DECLARE current_tid text;
        BEGIN
            current_tid := current_setting('app.current_tenant_id', true);
            IF current_tid IS NULL OR current_tid = '' THEN RETURN false; END IF;
            RETURN COALESCE(row_tenant = current_tid::uuid, false);
        EXCEPTION WHEN invalid_text_representation THEN RETURN false;
        END;
        $$
    """)
    _policies("app.tenant_filter_check(tenant_id)")
    op.execute("DROP FUNCTION app.tenant_filter_check()")


def downgrade() -> None:
    # Preserve refusal on rollback. Do not restore the previous exception-to-true helper.
    _policies("tenant_id::text = nullif(current_setting('app.current_tenant_id', true), '')")
    op.execute("DROP FUNCTION app.tenant_filter_check(uuid)")
    # Earlier migration downgrade expects this signature. It is deliberately deny-only.
    op.execute("""
        CREATE FUNCTION app.tenant_filter_check() RETURNS boolean
        LANGUAGE sql STABLE SECURITY INVOKER AS 'SELECT false'
    """)
