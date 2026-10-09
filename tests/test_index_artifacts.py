"""Real scoped file bytes and frozen plan validation; no models or DB substitutes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError as SchemaError
from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan
from ragos.storage.artifacts import ArtifactStore, canonical_json


def context():
    return TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="actor", trace_id="test")


def sample_plan(store, ctx, *, paginated=False):
    source_ref, source_digest = store.put_bytes(
        tenant_ctx=ctx, kind="sources", data=b"Original source"
    )
    version_id = uuid4()
    representation = FrozenRepresentation(
        schema_version=1,
        document_version_id=version_id,
        source_digest=source_digest,
        source_type="pdf" if paginated else "plain",
        parser_version="synthetic-fixture-parser",
        render_version="none",
        parent_unit="page" if paginated else "token_window",
        text="Original source",
        pages=({"page_no": 1, "start": 0, "end": 15},) if paginated else (),
    )
    ref, digest = store.put_bytes(
        tenant_ctx=ctx, kind="representations", data=representation.frozen_bytes()
    )
    parent_id = uuid4()
    base = {
        "body": "Original source",
        "start_char_idx": 0,
        "end_char_idx": 15,
        "element_type": "text",
    }
    if paginated:
        base.update(page_no=1, page_offset_start=0, page_offset_end=15)
    plan = IndexPlan(
        schema_version=1,
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=version_id,
        representation_id=uuid4(),
        index_id=uuid4(),
        tier="stable",
        source_ref=source_ref,
        source_digest=source_digest,
        representation_ref=ref,
        representation_digest=digest,
        chunker_version="fixture-chunker",
        chunker_config_digest="a" * 64,
        embedding_profile="hash-test",
        embedding_version="explicit-test",
        parent_unit=representation.parent_unit,
        representation_char_count=15,
        page_count=1 if paginated else None,
        expected_active_index_id=None,
        chunks=(
            {"id": parent_id, "parent_chunk_id": None, "is_parent": True, **base},
            {"id": uuid4(), "parent_chunk_id": parent_id, "is_parent": False, **base},
        ),
    )
    return plan, representation


def test_real_file_artifact_round_trip_is_immutable_and_digest_checked(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    reference, digest = store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original")
    assert store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original") == (reference, digest)
    assert store.read_bytes(tenant_ctx=ctx, reference=reference, digest=digest) == b"Original"
    (tmp_path / reference).write_bytes(b"Corrupt")
    with pytest.raises(ConflictError, match="digest"):
        store.read_bytes(tenant_ctx=ctx, reference=reference, digest=digest)
    with pytest.raises(ConflictError, match="digest"):
        store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original")


def test_artifact_ref_cannot_read_foreign_scope_or_symlink_escape(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    reference, digest = store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original")
    with pytest.raises(ValidationError, match="tenant"):
        store.read_bytes(tenant_ctx=context(), reference=reference, digest=digest)
    path = tmp_path / reference
    path.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"Original")
    path.symlink_to(outside)
    with pytest.raises(ValidationError, match="safe"):
        store.read_bytes(tenant_ctx=ctx, reference=reference, digest=digest)


def test_artifact_byte_budget_and_finite_json_are_enforced(tmp_path):
    store, ctx = ArtifactStore(tmp_path, max_bytes=4), context()
    with pytest.raises(ValidationError, match="budget"):
        store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"12345")
    with pytest.raises(ValidationError, match="finite"):
        canonical_json({"value": float("nan")})


@pytest.mark.parametrize("paginated", [False, True])
def test_plan_reads_actual_source_and_representation_artifacts(tmp_path, paginated):
    store, ctx = ArtifactStore(tmp_path), context()
    plan, representation = sample_plan(store, ctx, paginated=paginated)
    plan.verify_representation(representation)
    assert (
        store.read_bytes(tenant_ctx=ctx, reference=plan.source_ref, digest=plan.source_digest)
        == b"Original source"
    )
    loaded = FrozenRepresentation.model_validate(
        store.read_json(
            tenant_ctx=ctx, reference=plan.representation_ref, digest=plan.representation_digest
        )
    )
    plan.verify_representation(loaded)
    reference, digest = store.put_bytes(
        tenant_ctx=ctx, kind="index-plans", data=plan.frozen_bytes()
    )
    restored = IndexPlan.model_validate(
        store.read_json(tenant_ctx=ctx, reference=reference, digest=digest)
    )
    assert restored == plan and digest == plan.digest()
    upserts = restored.as_upserts(ctx)
    assert upserts[0].representation_id == plan.representation_id
    assert upserts[1].body == "Original source"


def test_same_length_text_tampering_is_not_accepted_as_original(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    plan, representation = sample_plan(store, ctx)
    data = plan.model_dump(mode="json")
    for chunk in data["chunks"]:
        chunk["body"] = "Different words"
    changed = IndexPlan.model_validate(data)
    with pytest.raises(ValidationError, match="original text"):
        changed.verify_representation(representation)


def test_page_coordinate_mapping_cannot_be_replaced_with_valid_global_span(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    plan, representation = sample_plan(store, ctx, paginated=True)
    data = plan.model_dump(mode="json")
    for chunk in data["chunks"]:
        chunk.update(page_offset_start=1, page_offset_end=16)
    changed = IndexPlan.model_validate(data)
    with pytest.raises(ValidationError, match="coordinates"):
        changed.verify_representation(representation)


def test_unknown_parent_and_repeated_chunk_id_are_rejected(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    plan, _ = sample_plan(store, ctx)
    data = plan.model_dump(mode="json")
    data["chunks"][1]["parent_chunk_id"] = str(uuid4())
    with pytest.raises(SchemaError, match="parent"):
        IndexPlan.model_validate(data)
    data["chunks"][1]["id"] = data["chunks"][0]["id"]
    with pytest.raises(SchemaError, match="unique"):
        IndexPlan.model_validate(data)


def test_fifo_artifact_is_rejected_without_waiting_for_a_writer(tmp_path):
    import os

    store, ctx = ArtifactStore(tmp_path), context()
    ref, digest = store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original")
    path = tmp_path / ref
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValidationError, match="regular file"):
        store.read_bytes(tenant_ctx=ctx, reference=ref, digest=digest)


def test_symlink_parent_is_refused_for_both_read_and_write(tmp_path):
    store, ctx = ArtifactStore(tmp_path), context()
    ref, digest = store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Original")
    parent = (tmp_path / ref).parent
    saved = parent.with_name("saved")
    parent.rename(saved)
    parent.symlink_to(saved, target_is_directory=True)
    with pytest.raises(ValidationError, match="safe"):
        store.read_bytes(tenant_ctx=ctx, reference=ref, digest=digest)
    with pytest.raises(ValidationError, match="safe"):
        store.put_bytes(tenant_ctx=ctx, kind="sources", data=b"Other")
