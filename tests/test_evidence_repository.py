"""Deterministic source-registration and evidence boundary probes.

These checks exercise rules and generated queries; real transactions, CAS races
and filesystem byte verification belong to the separate storage integration run.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from uuid import uuid4

import pytest
from ragos.core.errors import ConflictError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import ChunkManifest, DocumentRepresentation, DocumentVersion
from ragos.storage.models.base import utc_now
from ragos.storage.models.search import SearchChunkStable, SearchMemory
from ragos.storage.repositories.kb import (
    ChunkManifestRepository,
    DocumentRepresentationRepository,
    DocumentVersionRepository,
    _register_immutable,
    require_artifact_ref,
    require_digest,
    require_kb,
    require_pagination,
    require_reconciled_manifest,
)
from ragos.storage.repositories.search import (
    SearchChunkUpsert,
    SearchRepository,
    SearchTier,
    validate_chunk_coordinates,
    validate_parent_link,
)
from sqlalchemy.dialects import postgresql


def _ctx(**overrides) -> TenantContext:
    return TenantContext(
        tenant_id=uuid4(), tenant_slug="test", actor_id="trusted-user", trace_id="test", **overrides
    )


def _chunk(**overrides) -> SearchChunkUpsert:
    fields = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        parent_chunk_id=None,
        is_parent=True,
        layer="L1",
        lang="simple",
        title=None,
        headings_text=None,
        body="Frozen text",
        tags_text=None,
        metadata_json={},
        updated_at=utc_now(),
        start_char_idx=10,
        end_char_idx=21,
        representation_char_count=40,
        parent_unit="token_window",
    )
    fields.update(overrides)
    return SearchChunkUpsert(**fields)


class NoDatabase:
    def exec(self, _statement):
        raise AssertionError("invalid input reached the database")


@pytest.mark.parametrize("digest", ["", "legacy", "A" * 64, "a" * 63, "g" * 64, None])
def test_digest_cannot_invent_a_legacy_version(digest):
    with pytest.raises(ValidationError):
        require_digest(digest)


def test_scope_storage_key_and_digest_are_validated_without_io():
    tenant = uuid4()
    require_digest("a" * 64)
    require_artifact_ref(f"tenant/{tenant}/sources/sha256/" + "a" * 64, tenant)


@pytest.mark.parametrize(
    "suffix",
    [
        "../secret",
        "%2e%2e/secret",
        "a//b",
        "./x",
        "x?token=secret",
        "x#fragment",
        "x\\secret",
        "x\x00secret",
        "",
    ],
)
def test_artifact_reference_rejects_path_escape_and_noncanonical_keys(suffix):
    tenant = uuid4()
    with pytest.raises(ValidationError):
        require_artifact_ref(f"tenant/{tenant}/{suffix}", tenant)


def test_artifact_reference_rejects_cross_tenant_and_remote_urls():
    tenant = uuid4()
    for value in (f"tenant/{uuid4()}/source", "/tmp/source", "https://example.org/source"):
        with pytest.raises(ValidationError):
            require_artifact_ref(value, tenant)


@pytest.mark.parametrize(
    "policy",
    [
        dict(parent_unit="page", char_count=10, page_count=None),
        dict(parent_unit="page", char_count=10, page_count=0),
        dict(parent_unit="page", char_count=10, page_count=True),
        dict(parent_unit="token_window", char_count=10, page_count=1),
        dict(parent_unit="token_window", char_count=-1, page_count=None),
        dict(parent_unit="unknown", char_count=10, page_count=None),
    ],
)
def test_pagination_cannot_claim_unfrozen_pages(policy):
    with pytest.raises(ValidationError):
        require_pagination(**policy)


def test_kb_empty_acl_denies_before_lookup():
    with pytest.raises(PermissionDeniedError):
        require_kb(NoDatabase(), _ctx(allowed_kb_ids=[]), uuid4())


@pytest.mark.parametrize(
    "media_type",
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain"],
)
def test_nonpaginated_source_needs_frozen_rendering_for_pages(monkeypatch, media_type):
    ctx = _ctx()
    version = DocumentVersion(
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        source_digest="a" * 64,
        source_ref=f"tenant/{ctx.tenant_id}/source",
        media_type=media_type,
    )
    monkeypatch.setattr(
        DocumentVersionRepository, "require_in_document", lambda *args, **kwargs: version
    )
    repo = DocumentRepresentationRepository(NoDatabase(), ctx)
    with pytest.raises(ValidationError, match="stable source or frozen"):
        repo.register(
            kb_id=version.kb_id,
            document_id=version.document_id,
            document_version_id=version.id,
            representation_digest="b" * 64,
            representation_ref=f"tenant/{ctx.tenant_id}/representation",
            parser_version="parser-v1",
            render_version="none",
            parent_unit="page",
            char_count=10,
            page_count=1,
        )


class ExistingRowSession:
    def __init__(self, row):
        self.row = row

    def exec(self, _statement):
        return self

    def first(self):
        return self.row


def test_repeated_registration_never_changes_existing_reference():
    ctx = _ctx()
    version = DocumentVersion(
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        source_digest="a" * 64,
        source_ref=f"tenant/{ctx.tenant_id}/source",
        media_type="text/plain",
    )
    session = ExistingRowSession(version)
    fields = dict(source_digest=version.source_digest, source_ref=version.source_ref)
    assert _register_immutable(session, ctx, DocumentVersion, None, fields) is version
    with pytest.raises(ConflictError):
        _register_immutable(
            session,
            ctx,
            DocumentVersion,
            None,
            fields | {"source_ref": f"tenant/{ctx.tenant_id}/different"},
        )
    assert version.source_ref.endswith("/source")


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_char_idx": -1},
        {"end_char_idx": 50},
        {"end_char_idx": 10},
        {"start_char_idx": None},
        {"representation_char_count": None},
        {"body": "Different length"},
        {"page_no": 1},
        {"page_count": 1},
        {"parent_chunk_id": uuid4()},
        {"parent_unit": "unknown"},
    ],
)
def test_chunk_rejects_false_source_location(overrides):
    with pytest.raises(ValidationError):
        validate_chunk_coordinates(_chunk(**overrides))


def test_page_and_child_spans_use_raw_text_and_same_index():
    parent = _chunk(
        parent_unit="page", page_no=2, page_count=3, page_offset_start=0, page_offset_end=11
    )
    child = replace(
        parent,
        id=uuid4(),
        is_parent=False,
        parent_chunk_id=parent.id,
        body="text",
        start_char_idx=17,
        end_char_idx=21,
        page_offset_start=7,
        page_offset_end=11,
    )
    validate_chunk_coordinates(parent)
    validate_chunk_coordinates(child)
    validate_parent_link(child, parent)
    for incorrect in (
        replace(child, index_id=uuid4()),
        replace(child, representation_id=uuid4()),
        replace(child, page_no=1),
        replace(child, body="fake"),
    ):
        with pytest.raises(ValidationError):
            validate_parent_link(incorrect, parent)


def test_new_enterprise_chunk_cannot_silently_be_legacy():
    ctx = _ctx()
    repo = SearchRepository(NoDatabase(), ctx)
    with pytest.raises(ValidationError, match="complete immutable"):
        repo.upsert_chunks(
            tier=SearchTier.STABLE, chunks=[_chunk(tenant_id=ctx.tenant_id, index_id=None)]
        )


def test_default_read_joins_active_and_indexed_source_chain():
    ctx = _ctx()
    repo = SearchRepository(NoDatabase(), ctx)
    stmt = repo._scope(SearchChunkStable, tier=SearchTier.STABLE, scope_key=ctx.require_scope_key())
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "app.document_representation" in sql
    assert "app.chunk_manifest" in sql
    assert "chunk_stable.index_id = app.document_record.active_index_id" in sql
    assert "app.chunk_manifest.indexing_state = 'indexed'" in sql
    assert "app.chunk_manifest.plan_digest IS NOT NULL" in sql
    assert "app.chunk_manifest.vector_digest = app.chunk_manifest.pg_digest" in sql
    assert "app.chunk_manifest.reconciled_at IS NOT NULL" in sql
    assert "app.chunk_manifest.pg_digest ~" in sql
    assert "chunk_stable.document_version_id = app.chunk_manifest.document_version_id" in sql


def test_memory_query_does_not_join_or_invent_enterprise_versions():
    ctx = _ctx(user_id=uuid4())
    stmt = SearchRepository(NoDatabase(), ctx)._scope(
        SearchMemory, tier=SearchTier.MEMORY, scope_key=ctx.require_private_scope_key()
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "document_representation" not in sql
    assert "memory.user_id =" in sql
    memory = SearchMemory(id=uuid4(), tenant_id=ctx.tenant_id, user_id=ctx.user_id, body="private")
    hit = SearchRepository._hit(memory)
    assert hit.document_version_id is None
    assert hit.representation_id is None
    assert hit.index_id is None
    assert hit.parent_unit is None
    assert hit.representation_char_count is None


def test_source_repository_does_not_expose_a_mutable_add_path():
    ctx = _ctx()
    version = DocumentVersion(
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        source_digest="a" * 64,
        source_ref=f"tenant/{ctx.tenant_id}/source",
        media_type="text/plain",
    )
    with pytest.raises(ConflictError, match="validated contract"):
        DocumentVersionRepository(NoDatabase(), ctx).add(version)


def test_same_batch_duplicate_identity_is_refused_before_side_effects():
    ctx = _ctx()
    chunk = _chunk(tenant_id=ctx.tenant_id)
    with pytest.raises(ValidationError, match="duplicate chunk"):
        SearchRepository(NoDatabase(), ctx).upsert_chunks(
            tier=SearchTier.STABLE, chunks=[chunk, replace(chunk)]
        )


def test_same_uuid_does_not_make_a_foreign_parent_authorized():
    parent = _chunk()
    child = replace(parent, id=uuid4(), parent_chunk_id=parent.id, is_parent=False)
    with pytest.raises(ValidationError):
        validate_parent_link(child, replace(parent, tenant_id=uuid4()))


def _prepared_manifest(ctx, **overrides):
    fields = dict(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        representation_id=uuid4(),
        chunker_version="chunker-v1",
        chunker_config_digest="c" * 64,
        embedding_profile="embed-v1",
        embedding_version="weights-v1",
        plan_ref=f"tenant/{ctx.tenant_id}/plans/" + "a" * 64 + ".bin",
        plan_digest="a" * 64,
        chunk_total=1,
        parent_unit="token_window",
        parser_version="parser-v1",
    )
    fields.update(overrides)
    return ChunkManifest(**fields)


def _representation_for(manifest):
    return DocumentRepresentation(
        id=manifest.representation_id,
        tenant_id=manifest.tenant_id,
        kb_id=manifest.kb_id,
        document_id=manifest.document_id,
        document_version_id=manifest.document_version_id,
        representation_digest="b" * 64,
        representation_ref=f"tenant/{manifest.tenant_id}/representation/" + "b" * 64 + ".bin",
        parser_version=manifest.parser_version,
        render_version="none",
        parent_unit=manifest.parent_unit,
        char_count=40,
        page_count=manifest.page_count,
    )


def test_same_index_registration_is_idempotent_without_resetting_receipts(monkeypatch):
    import ragos.storage.repositories.kb as kb

    ctx = _ctx()
    manifest = _prepared_manifest(
        ctx,
        indexing_state="indexed",
        pg_digest="d" * 64,
        vector_digest="d" * 64,
        reconciled_at=utc_now(),
    )
    representation = _representation_for(manifest)
    monkeypatch.setattr(
        DocumentRepresentationRepository,
        "require_in_version",
        lambda *args, **kwargs: representation,
    )
    monkeypatch.setattr(kb, "require_manifest_contract", lambda *args, **kwargs: representation)
    fields = {
        name: getattr(manifest, name)
        for name in (
            "kb_id",
            "document_id",
            "document_version_id",
            "representation_id",
            "chunker_version",
            "chunker_config_digest",
            "embedding_profile",
            "embedding_version",
            "plan_ref",
            "plan_digest",
            "chunk_total",
        )
    }
    repo = ChunkManifestRepository(ExistingRowSession(manifest), ctx)
    assert repo.create(index_id=manifest.id, **fields) is manifest
    assert manifest.indexing_state == "indexed"
    assert manifest.reconciled_at is not None
    with pytest.raises(ConflictError):
        repo.create(index_id=manifest.id, **(fields | {"plan_digest": "e" * 64}))
    assert manifest.plan_digest == "a" * 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"pg_digest": None},
        {"vector_digest": None},
        {"pg_digest": ""},
        {"vector_digest": "e" * 64},
        {"reconciled_at": None},
        {"reconciled_at": datetime(2026, 10, 9)},
        {"indexing_state": "pending"},
    ],
)
def test_indexed_flag_alone_cannot_publish_a_plan(overrides):
    ctx = _ctx()
    manifest = _prepared_manifest(
        ctx,
        **(
            dict(
                indexing_state="indexed",
                pg_digest="d" * 64,
                vector_digest="d" * 64,
                reconciled_at=utc_now(),
            )
            | overrides
        ),
    )
    with pytest.raises((ValidationError, ConflictError)):
        require_reconciled_manifest(manifest, expected_projection_digest="d" * 64)


def test_receipts_must_match_frozen_plan_projection():
    manifest = _prepared_manifest(
        _ctx(),
        indexing_state="indexed",
        pg_digest="d" * 64,
        vector_digest="d" * 64,
        reconciled_at=utc_now(),
    )
    require_reconciled_manifest(manifest, expected_projection_digest="d" * 64)
    with pytest.raises(ConflictError):
        require_reconciled_manifest(manifest, expected_projection_digest="e" * 64)


class SequenceResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value

    def all(self):
        return self.value


class ReadbackSession:
    """Mock readback adapter; does not demonstrate database transactions or RLS."""

    def __init__(self, manifest, rows):
        self.results = iter([manifest, rows])
        self.statements = []

    def exec(self, statement):
        self.statements.append(statement)
        return SequenceResult(next(self.results))


def _staging_row(manifest):
    chunk = _chunk(
        tenant_id=manifest.tenant_id,
        kb_id=manifest.kb_id,
        document_id=manifest.document_id,
        document_version_id=manifest.document_version_id,
        representation_id=manifest.representation_id,
        index_id=manifest.id,
    )
    fields = asdict(chunk)
    for field in ("representation_char_count", "parent_unit", "user_id"):
        fields.pop(field)
    return SearchChunkStable(**fields)


def test_reconciliation_reads_unpublished_index_without_public_fallback(monkeypatch):
    import ragos.storage.repositories.search as search

    ctx = _ctx()
    manifest = _prepared_manifest(ctx, indexing_state="indexing")
    representation = _representation_for(manifest)
    row = _staging_row(manifest)
    monkeypatch.setattr(search, "require_manifest_contract", lambda *args, **kwargs: representation)
    session = ReadbackSession(manifest, [row])
    projections = SearchRepository(session, ctx).read_index_for_reconciliation(
        scope_key=ctx.require_scope_key(), index_id=manifest.id, tier=SearchTier.STABLE
    )
    assert len(projections) == 1
    assert projections[0].index_id == manifest.id
    assert projections[0].chunk_id == row.id
    assert projections[0].representation_char_count == representation.char_count
    sql = str(
        session.statements[-1].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "active_index_id" not in sql
    assert "chunk_stable.index_id =" in sql
    assert "chunk_stable.tenant_id =" in sql


@pytest.mark.parametrize("wrong", ["owner", "missing", "extra"])
def test_reconciliation_cannot_attest_a_filtered_subset(monkeypatch, wrong):
    import ragos.storage.repositories.search as search

    ctx = _ctx()
    manifest = _prepared_manifest(ctx)
    representation = _representation_for(manifest)
    row = _staging_row(manifest)
    rows = [row]
    if wrong == "owner":
        row.document_id = uuid4()
    elif wrong == "missing":
        rows = []
    else:
        rows.append(_staging_row(manifest))
    monkeypatch.setattr(search, "require_manifest_contract", lambda *args, **kwargs: representation)
    with pytest.raises((PermissionDeniedError, ConflictError)):
        SearchRepository(ReadbackSession(manifest, rows), ctx).read_index_for_reconciliation(
            scope_key=ctx.require_scope_key(), index_id=manifest.id, tier=SearchTier.STABLE
        )


def test_reconciliation_refuses_foreign_scope_and_private_memory_before_io():
    ctx = _ctx()
    repo = SearchRepository(NoDatabase(), ctx)
    with pytest.raises(PermissionDeniedError):
        repo.read_index_for_reconciliation(
            scope_key=f"tenant/{uuid4()}", index_id=uuid4(), tier=SearchTier.STABLE
        )
    with pytest.raises(ValidationError):
        repo.read_index_for_reconciliation(
            scope_key=ctx.require_scope_key(), index_id=uuid4(), tier=SearchTier.MEMORY
        )
