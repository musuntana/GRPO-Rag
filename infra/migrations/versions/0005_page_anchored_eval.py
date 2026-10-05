"""page anchoring, eval cases, retriever profile versions, reranker artifacts

Revision ID: 0005_page_anchored_eval
Revises: 0004_model_gateway
Create Date: 2026-10-04

Implements ADR-0006 (page-anchored retrieval + verifiable citations) and
ADR-0007 (per-tenant offline reranker training).

search.* additions
    page_no, page_count, page_offset_start, page_offset_end, element_type,
    start_char_idx, end_char_idx, is_parent, brand_id, source_type,
    security_level, page_image_ref, and user_id on search.memory.

app.chunk_manifest additions
    parent_unit, page_count, parser_version, chunker_version,
    embedding_profile.

app additions
    retriever_profile_version, eval_case, eval_run, grader_validation,
    reranker_artifact; model_profile.artifact_ref.

Backfill note: existing rows get NULL page columns and
``parent_unit = 'token_window'``, which is the correct value for everything
indexed before this revision. Re-index to adopt page anchoring.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_page_anchored_eval"
down_revision = "0004_model_gateway"
branch_labels = None
depends_on = None

APP_SCHEMA = "app"
SEARCH_SCHEMA = "search"

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)

_SEARCH_TABLES = ("chunk_stable", "chunk_hot", "memory")

_SEARCH_COLUMNS = (
    # Page anchoring (architecture 7.4.1). NULL under parent_unit=token_window.
    ("page_no", sa.Integer(), True),
    ("page_count", sa.Integer(), True),
    ("page_offset_start", sa.Integer(), True),
    ("page_offset_end", sa.Integer(), True),
    ("element_type", sa.String(length=16), False, sa.text("'text'")),
    # Document-global offsets. Citation linking depends on these.
    ("start_char_idx", sa.Integer(), True),
    ("end_char_idx", sa.Integer(), True),
    ("is_parent", sa.Boolean(), False, sa.text("false")),
    ("brand_id", _UUID, True),
    ("source_type", sa.String(length=32), False, sa.text("'upload'")),
    ("security_level", sa.String(length=16), False, sa.text("'internal'")),
    ("page_image_ref", sa.String(length=1024), True),
)

_APP_TIMESTAMPS = (
    sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
)


def _new_app_table(name: str, *columns: sa.Column, constraints=()) -> None:
    op.create_table(name, *columns, *_APP_TIMESTAMPS, *constraints, schema=APP_SCHEMA)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. search schema: page anchoring + citation offsets
    # ------------------------------------------------------------------
    for table in _SEARCH_TABLES:
        for spec in _SEARCH_COLUMNS:
            name, type_, nullable = spec[0], spec[1], spec[2]
            server_default = spec[3] if len(spec) > 3 else None
            op.add_column(
                table,
                sa.Column(name, type_, nullable=nullable, server_default=server_default),
                schema=SEARCH_SCHEMA,
            )

    # Per-user attribution on memory. Without a user dimension on the row,
    # per-user memory export and deletion are not expressible.
    op.add_column("memory", sa.Column("user_id", _UUID, nullable=True), schema=SEARCH_SCHEMA)

    op.create_index(
        "ix_chunk_stable_page", "chunk_stable", ["tenant_id", "document_id", "page_no"],
        unique=False, schema=SEARCH_SCHEMA,
    )
    op.create_index(
        "ix_chunk_hot_page", "chunk_hot", ["tenant_id", "document_id", "page_no"],
        unique=False, schema=SEARCH_SCHEMA,
    )
    op.create_index(
        "ix_search_memory_user", "memory", ["tenant_id", "user_id"],
        unique=False, schema=SEARCH_SCHEMA,
    )
    for table in _SEARCH_TABLES:
        op.create_index(f"ix_{table}_page_no", table, ["page_no"], unique=False, schema=SEARCH_SCHEMA)
        op.create_index(f"ix_{table}_is_parent", table, ["is_parent"], unique=False, schema=SEARCH_SCHEMA)

    # ------------------------------------------------------------------
    # 2. app.chunk_manifest: record the hierarchy strategy on the index
    # ------------------------------------------------------------------
    op.add_column(
        "chunk_manifest",
        sa.Column("parent_unit", sa.String(length=16), nullable=False, server_default=sa.text("'token_window'")),
        schema=APP_SCHEMA,
    )
    op.add_column("chunk_manifest", sa.Column("page_count", sa.Integer(), nullable=True), schema=APP_SCHEMA)
    op.add_column("chunk_manifest", sa.Column("parser_version", sa.String(length=64), nullable=True), schema=APP_SCHEMA)
    op.add_column("chunk_manifest", sa.Column("chunker_version", sa.String(length=64), nullable=True), schema=APP_SCHEMA)
    op.add_column("chunk_manifest", sa.Column("embedding_profile", sa.String(length=64), nullable=True), schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 3. app.retriever_profile_version — frozen retrieval configuration
    # ------------------------------------------------------------------
    _new_app_table(
        "retriever_profile_version",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("query_strategy", sa.String(length=32), nullable=False, server_default=sa.text("'direct'")),
        sa.Column("recall_channels", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("join_mode", sa.String(length=32), nullable=False, server_default=sa.text("'reciprocal_rank_fusion'")),
        sa.Column("channel_budgets", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fusion_config", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("crag_thresholds", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("rerank_model_profile", sa.String(length=128), nullable=True),
        sa.Column("citation_verify_semantic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("top_k_recall", sa.Integer(), nullable=False, server_default=sa.text("50")),
        sa.Column("top_k_rerank", sa.Integer(), nullable=False, server_default=sa.text("20")),
        sa.Column("top_k_generate", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_rpv_tenant_id_tenant"),
        sa.UniqueConstraint("tenant_id", "profile_id", "version", name="uq_retriever_profile_version"),
    )
    op.create_index("ix_rpv_tenant_id", "retriever_profile_version", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_rpv_profile_id", "retriever_profile_version", ["profile_id"], unique=False, schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 4. app.eval_case — graded ground truth (architecture 13.3.0)
    # ------------------------------------------------------------------
    _new_app_table(
        "eval_case",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("kb_id", _UUID, nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("conversation_turns", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # Graded relevance per document. NOT a boolean: grade 1 is what makes
        # hard-negative mining useful rather than harmful.
        sa.Column("gold_evidence", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        sa.Column("answerable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("supported_claims", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_state", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("labeler", sa.String(length=255), nullable=True),
        sa.Column("split", sa.String(length=16), nullable=False, server_default=sa.text("'dev'")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_eval_case_tenant_id_tenant"),
    )
    op.create_index("ix_eval_case_tenant_id", "eval_case", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_eval_case_kb_id", "eval_case", ["kb_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_eval_case_split", "eval_case", ["tenant_id", "split"], unique=False, schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 5. app.eval_run — frozen bundle + metrics
    # ------------------------------------------------------------------
    _new_app_table(
        "eval_run",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("bundle_id", sa.String(length=128), nullable=False),
        sa.Column("bundle", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metrics", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("case_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("split", sa.String(length=16), nullable=False, server_default=sa.text("'holdout'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'completed'")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_eval_run_tenant_id_tenant"),
    )
    op.create_index("ix_eval_run_tenant_id", "eval_run", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_eval_run_bundle_id", "eval_run", ["bundle_id"], unique=False, schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 6. app.grader_validation — the rails grader is itself validated
    # ------------------------------------------------------------------
    _new_app_table(
        "grader_validation",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("rails_prompt_version", sa.String(length=64), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("agreement", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("confusion", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_grader_validation_tenant_id_tenant"),
    )
    op.create_index("ix_gv_tenant_id", "grader_validation", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_gv_version", "grader_validation", ["rails_prompt_version"], unique=False, schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 7. app.reranker_artifact — trained artifacts, tenant-namespaced
    # ------------------------------------------------------------------
    _new_app_table(
        "reranker_artifact",
        sa.Column("id", _UUID, primary_key=True, nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # sft is the shippable baseline; grpo exists only if it beat it.
        sa.Column("method", sa.String(length=32), nullable=False, server_default=sa.text("'sft'")),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("artifact_ref", sa.String(length=1024), nullable=False),
        sa.Column("source_bundle", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("eval_metrics", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_promoted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["tenant_id"], [f"{APP_SCHEMA}.tenant.id"], name="fk_reranker_artifact_tenant_id_tenant"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_reranker_artifact_tenant_version"),
    )
    op.create_index("ix_reranker_artifact_tenant_id", "reranker_artifact", ["tenant_id"], unique=False, schema=APP_SCHEMA)
    op.create_index("ix_reranker_artifact_is_promoted", "reranker_artifact", ["is_promoted"], unique=False, schema=APP_SCHEMA)

    # ------------------------------------------------------------------
    # 8. model_profile.artifact_ref — a trained reranker is still a profile
    # ------------------------------------------------------------------
    op.add_column(
        "model_profile",
        sa.Column("artifact_ref", sa.String(length=1024), nullable=True),
        schema=APP_SCHEMA,
    )

    # ------------------------------------------------------------------
    # 9. RLS on every new tenant-scoped table (defense in depth, not the
    #    enforcement point — that is tenant_ctx in application code)
    # ------------------------------------------------------------------
    for table_name in (
        "retriever_profile_version",
        "eval_case",
        "eval_run",
        "grader_validation",
        "reranker_artifact",
    ):
        op.execute(f'ALTER TABLE {APP_SCHEMA}."{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f"CREATE POLICY tenant_isolation_{table_name} "
            f'ON {APP_SCHEMA}."{table_name}" '
            f"FOR ALL "
            f"USING ({APP_SCHEMA}.tenant_filter_check()) "
            f"WITH CHECK ({APP_SCHEMA}.tenant_filter_check())"
        )


def downgrade() -> None:
    for table_name in (
        "reranker_artifact",
        "grader_validation",
        "eval_run",
        "eval_case",
        "retriever_profile_version",
    ):
        op.execute(f'ALTER TABLE {APP_SCHEMA}."{table_name}" DISABLE ROW LEVEL SECURITY')
        op.drop_table(table_name, schema=APP_SCHEMA)

    op.drop_column("model_profile", "artifact_ref", schema=APP_SCHEMA)

    for column in (
        "embedding_profile",
        "chunker_version",
        "parser_version",
        "page_count",
        "parent_unit",
    ):
        op.drop_column("chunk_manifest", column, schema=APP_SCHEMA)

    op.drop_column("memory", "user_id", schema=SEARCH_SCHEMA)
    for table in _SEARCH_TABLES:
        op.drop_index(f"ix_{table}_is_parent", table_name=table, schema=SEARCH_SCHEMA)
        op.drop_index(f"ix_{table}_page_no", table_name=table, schema=SEARCH_SCHEMA)
    op.drop_index("ix_search_memory_user", table_name="memory", schema=SEARCH_SCHEMA)
    op.drop_index("ix_chunk_hot_page", table_name="chunk_hot", schema=SEARCH_SCHEMA)
    op.drop_index("ix_chunk_stable_page", table_name="chunk_stable", schema=SEARCH_SCHEMA)

    # Alembic's signature is drop_column(table_name, column_name, *, schema).
    # Passing the column first and the table by keyword raises TypeError at
    # downgrade time, which is how a rollback fails halfway through.
    for spec in reversed(_SEARCH_COLUMNS):
        op.drop_column("memory", spec[0], schema=SEARCH_SCHEMA)
        op.drop_column("chunk_hot", spec[0], schema=SEARCH_SCHEMA)
        op.drop_column("chunk_stable", spec[0], schema=SEARCH_SCHEMA)