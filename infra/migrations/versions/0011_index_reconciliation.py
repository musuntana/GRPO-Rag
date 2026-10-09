"""Persist independent search-store readback facts on the existing manifest."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_index_reconciliation"
down_revision = "0010_immutable_evidence_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunk_manifest", sa.Column("pg_digest", sa.String(64)), schema="app")
    op.add_column("chunk_manifest", sa.Column("vector_digest", sa.String(64)), schema="app")
    op.add_column(
        "chunk_manifest", sa.Column("reconciled_at", sa.DateTime(timezone=True)), schema="app"
    )

    # Prior candidates without receipts remain stored, but cannot stay publicly active.
    op.execute(
        "UPDATE app.chunk_manifest SET indexing_state='failed', "
        "error_message='Index requires verified two-store readback' "
        "WHERE indexing_state='indexed' AND "
        "(pg_digest IS NULL OR vector_digest IS NULL OR reconciled_at IS NULL "
        "OR pg_digest IS DISTINCT FROM vector_digest)"
    )
    op.execute(
        "UPDATE app.document_record SET active_index_id=NULL, ingest_status='failed' "
        "WHERE active_index_id IN (SELECT id FROM app.chunk_manifest "
        "WHERE indexing_state='failed' AND reconciled_at IS NULL)"
    )
    op.create_check_constraint(
        "ck_manifest_readback_publication",
        "chunk_manifest",
        "indexing_state <> 'indexed' OR (pg_digest IS NOT NULL AND "
        "vector_digest IS NOT NULL AND vector_digest = pg_digest "
        "AND reconciled_at IS NOT NULL)",
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.reject_manifest_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF ROW(NEW.id, NEW.tenant_id, NEW.kb_id, NEW.document_id, NEW.document_version_id,
                   NEW.representation_id, NEW.chunker_version, NEW.chunker_config_digest,
                   NEW.embedding_profile, NEW.embedding_version, NEW.plan_ref, NEW.plan_digest,
                   NEW.parent_unit, NEW.page_count, NEW.parser_version, NEW.chunk_total)
            IS DISTINCT FROM
               ROW(OLD.id, OLD.tenant_id, OLD.kb_id, OLD.document_id, OLD.document_version_id,
                   OLD.representation_id, OLD.chunker_version, OLD.chunker_config_digest,
                   OLD.embedding_profile, OLD.embedding_version, OLD.plan_ref, OLD.plan_digest,
                   OLD.parent_unit, OLD.page_count, OLD.parser_version, OLD.chunk_total) THEN
                RAISE EXCEPTION 'immutable index plan cannot be changed' USING ERRCODE='23514';
            END IF;
            IF NEW.indexing_state='indexed' AND
               (NEW.pg_digest IS NULL OR NEW.vector_digest IS NULL OR NEW.reconciled_at IS NULL
                OR NEW.pg_digest IS DISTINCT FROM NEW.vector_digest) THEN
                RAISE EXCEPTION 'index publication requires readback facts' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER immutable_manifest BEFORE UPDATE ON app.chunk_manifest
        FOR EACH ROW EXECUTE FUNCTION app.reject_manifest_rewrite()
    """)
    op.execute("""
        CREATE FUNCTION search.reject_chunk_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF (to_jsonb(NEW)-'updated_at'-'expires_at'-'search_vector') IS DISTINCT FROM
               (to_jsonb(OLD)-'updated_at'-'expires_at'-'search_vector') THEN
                RAISE EXCEPTION 'immutable evidence chunk cannot be changed' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$
    """)
    for table in ("chunk_stable", "chunk_hot"):
        op.execute(
            f'CREATE TRIGGER immutable_chunk BEFORE UPDATE ON search."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION search.reject_chunk_rewrite()"
        )

    op.execute("""
        CREATE FUNCTION search.guard_published_chunk_set() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE owner_index uuid; owner_tenant uuid; index_state text; replay_exists boolean;
        BEGIN
            IF TG_OP='DELETE' THEN
                owner_index := OLD.index_id; owner_tenant := OLD.tenant_id;
            ELSE
                owner_index := NEW.index_id; owner_tenant := NEW.tenant_id;
            END IF;
            IF owner_index IS NULL THEN
                IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END IF;
            SELECT indexing_state INTO index_state FROM app.chunk_manifest
                WHERE id=owner_index AND tenant_id=owner_tenant FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'index owner is unavailable' USING ERRCODE='23514';
            END IF;
            IF index_state='indexed' THEN
                IF TG_OP='DELETE' THEN
                    RAISE EXCEPTION 'published evidence cannot be deleted'
                        USING ERRCODE='23514';
                END IF;
                EXECUTE format(
                    'SELECT EXISTS(SELECT 1 FROM search.%I WHERE id=$1 AND tenant_id=$2)',
                    TG_TABLE_NAME) INTO replay_exists USING NEW.id, NEW.tenant_id;
                IF NOT replay_exists THEN
                    RAISE EXCEPTION 'published index cannot gain new evidence'
                        USING ERRCODE='23514';
                END IF;
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END; $$
    """)
    for table in ("chunk_stable", "chunk_hot"):
        op.execute(
            f'CREATE TRIGGER published_chunk_set BEFORE INSERT OR DELETE ON search."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION search.guard_published_chunk_set()"
        )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM app.chunk_manifest WHERE pg_digest IS NOT NULL "
                "OR vector_digest IS NOT NULL OR reconciled_at IS NOT NULL "
                "OR indexing_state='indexed')"
            )
        )
        .scalar()
    )
    if exists:
        raise RuntimeError("reconciliation downgrade refused: readback facts must be preserved")
    op.drop_constraint(
        "ck_manifest_readback_publication", "chunk_manifest", type_="check", schema="app"
    )
    for table in ("chunk_stable", "chunk_hot"):
        op.execute(f'DROP TRIGGER immutable_chunk ON search."{table}"')
        op.execute(f'DROP TRIGGER published_chunk_set ON search."{table}"')
    op.execute("DROP FUNCTION search.reject_chunk_rewrite()")
    op.execute("DROP FUNCTION search.guard_published_chunk_set()")
    op.execute("DROP TRIGGER immutable_manifest ON app.chunk_manifest")
    op.execute("DROP FUNCTION app.reject_manifest_rewrite()")
    for column in ("reconciled_at", "vector_digest", "pg_digest"):
        op.drop_column("chunk_manifest", column, schema="app")
