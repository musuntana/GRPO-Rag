"""Immutable source/representation identities; retain legacy rows as unversioned."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0010_immutable_evidence_lineage"
down_revision = "0009_fail_closed_rls"
branch_labels = None
depends_on = None


def _base_columns():
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("kb_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.alter_column(
        "chunk_manifest",
        "parser_version",
        type_=sa.String(128),
        existing_type=sa.String(64),
        schema="app",
    )
    op.create_unique_constraint(
        "uq_document_owner_id", "document_record", ["tenant_id", "kb_id", "id"], schema="app"
    )
    op.add_column("document_record", sa.Column("active_index_id", UUID(as_uuid=True)), schema="app")
    op.create_table(
        "document_version",
        *_base_columns(),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "kb_id", "document_id", "id", name="uq_document_version_owner_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "source_digest", name="uq_document_version_digest"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kb_id", "document_id"],
            [
                "app.document_record.tenant_id",
                "app.document_record.kb_id",
                "app.document_record.id",
            ],
            name="fk_document_version_owner",
        ),
        schema="app",
    )
    op.create_index(
        "ix_document_version_tenant_id", "document_version", ["tenant_id"], schema="app"
    )
    op.create_table(
        "document_representation",
        *_base_columns(),
        sa.Column("document_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("representation_digest", sa.String(64), nullable=False),
        sa.Column("representation_ref", sa.String(1024), nullable=False),
        sa.Column("parser_version", sa.String(128), nullable=False),
        sa.Column("render_version", sa.String(128), nullable=False),
        sa.Column("parent_unit", sa.String(16), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.UniqueConstraint(
            "tenant_id",
            "kb_id",
            "document_id",
            "document_version_id",
            "id",
            name="uq_representation_owner_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "representation_digest",
            "parser_version",
            "render_version",
            name="uq_representation_content_profile",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kb_id", "document_id", "document_version_id"],
            [
                "app.document_version.tenant_id",
                "app.document_version.kb_id",
                "app.document_version.document_id",
                "app.document_version.id",
            ],
            name="fk_representation_version_owner",
        ),
        schema="app",
    )
    op.create_index(
        "ix_document_representation_tenant_id",
        "document_representation",
        ["tenant_id"],
        schema="app",
    )
    for name, type_ in (
        ("document_version_id", UUID(as_uuid=True)),
        ("representation_id", UUID(as_uuid=True)),
        ("chunker_config_digest", sa.String(64)),
        ("embedding_version", sa.String(128)),
        ("plan_ref", sa.String(1024)),
        ("plan_digest", sa.String(64)),
        ("legacy_indexing_state", sa.String(32)),
    ):
        op.add_column("chunk_manifest", sa.Column(name, type_), schema="app")
    op.execute(
        "UPDATE app.chunk_manifest SET legacy_indexing_state=indexing_state, "
        "indexing_state='unversioned' WHERE document_version_id IS NULL"
    )
    op.create_unique_constraint(
        "uq_manifest_owner_id",
        "chunk_manifest",
        ["tenant_id", "kb_id", "document_id", "id"],
        schema="app",
    )
    op.create_unique_constraint(
        "uq_manifest_lineage_id",
        "chunk_manifest",
        ["tenant_id", "kb_id", "document_id", "document_version_id", "representation_id", "id"],
        schema="app",
    )
    op.create_foreign_key(
        "fk_manifest_representation_owner",
        "chunk_manifest",
        "document_representation",
        ["tenant_id", "kb_id", "document_id", "document_version_id", "representation_id"],
        ["tenant_id", "kb_id", "document_id", "document_version_id", "id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_foreign_key(
        "fk_document_active_index_owner",
        "document_record",
        "chunk_manifest",
        ["tenant_id", "kb_id", "id", "active_index_id"],
        ["tenant_id", "kb_id", "document_id", "id"],
        source_schema="app",
        referent_schema="app",
    )
    for table, suffix in (("chunk_stable", "stable"), ("chunk_hot", "hot")):
        for name in ("document_version_id", "representation_id", "index_id"):
            op.add_column(table, sa.Column(name, UUID(as_uuid=True)), schema="search")
        op.create_foreign_key(
            f"fk_{suffix}_manifest_lineage",
            table,
            "chunk_manifest",
            [
                "tenant_id",
                "kb_id",
                "document_id",
                "document_version_id",
                "representation_id",
                "index_id",
            ],
            ["tenant_id", "kb_id", "document_id", "document_version_id", "representation_id", "id"],
            source_schema="search",
            referent_schema="app",
        )
    for table in ("document_version", "document_representation"):
        op.execute(f'ALTER TABLE app."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_isolation_{table} ON app."{table}" FOR ALL '
            "USING (app.tenant_filter_check(tenant_id)) "
            "WITH CHECK (app.tenant_filter_check(tenant_id))"
        )
    op.execute("""
        CREATE FUNCTION app.reject_source_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF (to_jsonb(NEW)-'updated_at') IS DISTINCT FROM (to_jsonb(OLD)-'updated_at') THEN
                RAISE EXCEPTION 'immutable source identity cannot be modified'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$
    """)
    for table in ("document_version", "document_representation"):
        op.execute(
            f'CREATE TRIGGER immutable_source BEFORE UPDATE ON app."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION app.reject_source_mutation()"
        )


def downgrade() -> None:
    connection = op.get_bind()
    # Refuse filtered existence checks: non-owner without bypass gets an error.
    op.execute("SET LOCAL row_security = off")
    if connection.execute(sa.text("SELECT EXISTS(SELECT 1 FROM app.document_version)")).scalar():
        raise RuntimeError("lineage downgrade refused: immutable source records must be preserved")
    for table, suffix in (("chunk_stable", "stable"), ("chunk_hot", "hot")):
        op.drop_constraint(
            f"fk_{suffix}_manifest_lineage", table, type_="foreignkey", schema="search"
        )
        for name in ("index_id", "representation_id", "document_version_id"):
            op.drop_column(table, name, schema="search")
    op.drop_constraint(
        "fk_document_active_index_owner", "document_record", type_="foreignkey", schema="app"
    )
    op.drop_constraint(
        "fk_manifest_representation_owner", "chunk_manifest", type_="foreignkey", schema="app"
    )
    op.drop_constraint("uq_manifest_lineage_id", "chunk_manifest", type_="unique", schema="app")
    op.drop_constraint("uq_manifest_owner_id", "chunk_manifest", type_="unique", schema="app")
    op.execute(
        "UPDATE app.chunk_manifest SET indexing_state=legacy_indexing_state "
        "WHERE indexing_state='unversioned' AND legacy_indexing_state IS NOT NULL"
    )
    for name in (
        "legacy_indexing_state",
        "plan_digest",
        "plan_ref",
        "embedding_version",
        "chunker_config_digest",
        "representation_id",
        "document_version_id",
    ):
        op.drop_column("chunk_manifest", name, schema="app")
    op.alter_column(
        "chunk_manifest",
        "parser_version",
        type_=sa.String(64),
        existing_type=sa.String(128),
        schema="app",
    )
    op.drop_table("document_representation", schema="app")
    op.drop_table("document_version", schema="app")
    op.execute("DROP FUNCTION app.reject_source_mutation()")
    op.drop_column("document_record", "active_index_id", schema="app")
    op.drop_constraint("uq_document_owner_id", "document_record", type_="unique", schema="app")
