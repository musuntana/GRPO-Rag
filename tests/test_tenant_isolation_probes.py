"""Cross-tenant probes: the release gate, not optional hardening.

Architecture §5.3.1. Every probe here asserts that a missing or foreign scope
fails closed. A probe that passes because the code path was never exercised is
worse than no probe, so each test names the failure it is defending against.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext, build_scope_key
from ragos.storage.repositories.search import SearchChunkUpsert
from ragos.storage.vector import build_filter, build_filter_spec


def _ctx(**overrides) -> TenantContext:
    fields = {
        "tenant_id": uuid4(),
        "tenant_slug": "acme",
        "actor_id": "u_1",
        "trace_id": "trace-1",
    }
    fields.update(overrides)
    return TenantContext(**fields)


def _chunk(tenant_id, **overrides) -> SearchChunkUpsert:
    from datetime import UTC, datetime

    fields = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "kb_id": uuid4(),
        "document_id": uuid4(),
        "parent_chunk_id": None,
        "layer": "L1",
        "lang": "simple",
        "title": None,
        "headings_text": None,
        "body": "text",
        "tags_text": None,
        "metadata_json": {},
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    fields.update(
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        representation_char_count=1000,
        parent_unit="token_window",
        start_char_idx=0,
        end_char_idx=len(fields["body"]),
    )
    return SearchChunkUpsert(**fields)


# -- scope key composition --------------------------------------------------


def test_scope_key_is_derived_and_shared_within_a_tenant() -> None:
    tenant = uuid4()
    first = _ctx(tenant_id=tenant, actor_id="u_1")
    second = _ctx(tenant_id=tenant, actor_id="u_2")
    assert first.scope_key == second.scope_key == f"tenant/{tenant}"
    assert first.require_private_scope_key() != second.require_private_scope_key()


def test_scope_key_cannot_be_supplied_or_overwritten() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _ctx(scope_key=f"tenant/{uuid4()}")
    with pytest.raises(ValidationError):
        _ctx().scope_key = ""


def test_scope_encoding_separates_actors_with_reserved_characters() -> None:
    tenant = uuid4()
    assert build_scope_key(tenant_id=tenant, actor_id="a/b") != build_scope_key(
        tenant_id=tenant, actor_id="a%2Fb"
    )


def test_require_scope_key_fails_closed_for_invalid_identity() -> None:
    with pytest.raises(PermissionDeniedError, match="unscoped"):
        build_scope_key(tenant_id=None)


def test_thread_id_derivation_refuses_a_context_with_no_tenant() -> None:
    ctx = _ctx().model_copy(update={"tenant_id": None})
    with pytest.raises(PermissionDeniedError, match="unscoped"):
        ctx.build_thread_id("wf", "run")


# -- repository -------------------------------------------------------------


def test_search_repository_refuses_construction_without_a_scope_key() -> None:
    """The repository is built with a validated identity or not at all."""
    from ragos.storage.repositories.search import SearchRepository

    ctx = _ctx().model_copy(update={"tenant_id": None})

    with pytest.raises(PermissionDeniedError, match="unscoped"):
        SearchRepository(session=None, tenant_ctx=ctx)


def test_search_refuses_a_foreign_scope_key() -> None:
    """A caller cannot widen a query by passing another tenant's key."""
    from ragos.storage.repositories.search import SearchRepository, SearchTier

    ctx = _ctx()
    repo = SearchRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(PermissionDeniedError, match="does not match"):
        repo.search(
            tier=SearchTier.STABLE,
            query="revenue",
            scope_key=f"tenant/{uuid4()}",
        )


def test_search_refuses_an_empty_scope_key() -> None:
    from ragos.storage.repositories.search import SearchRepository, SearchTier

    ctx = _ctx()
    repo = SearchRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(PermissionDeniedError, match="required"):
        repo.search(tier=SearchTier.STABLE, query="revenue", scope_key="")


def test_the_retriever_profile_repository_refuses_construction_without_a_scope_key() -> None:
    """A frozen retrieval profile is per-tenant configuration; an unscoped read of
    one would hand tenant A the retrieval behaviour of tenant B."""
    from ragos.storage.repositories.retriever import RetrieverProfileRepository

    ctx = _ctx().model_copy(update={"tenant_id": None})

    with pytest.raises(PermissionDeniedError, match="unscoped"):
        RetrieverProfileRepository(session=None, tenant_ctx=ctx)


def test_the_retriever_profile_repository_refuses_a_foreign_scope_key() -> None:
    from ragos.storage.repositories.retriever import RetrieverProfileRepository

    ctx = _ctx()
    repo = RetrieverProfileRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(PermissionDeniedError, match="does not match"):
        repo.require_latest_version(scope_key=f"tenant/{uuid4()}", profile_id=uuid4())


def test_the_retriever_profile_repository_refuses_an_empty_scope_key() -> None:
    from ragos.storage.repositories.retriever import RetrieverProfileRepository

    ctx = _ctx()
    repo = RetrieverProfileRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(PermissionDeniedError, match="required"):
        repo.list_profiles(scope_key="")


def test_upsert_for_another_tenant_is_denied_not_rewritten() -> None:
    from ragos.storage.repositories.search import SearchRepository, SearchTier

    ctx = _ctx()
    repo = SearchRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(PermissionDeniedError, match="tenant does not match"):
        repo.upsert_chunks(
            tier=SearchTier.STABLE,
            chunks=[_chunk(uuid4())],
        )


def test_an_unknown_element_type_is_rejected_rather_than_indexed() -> None:
    from ragos.storage.repositories.search import SearchRepository, SearchTier

    ctx = _ctx()
    repo = SearchRepository(session=None, tenant_ctx=ctx)

    with pytest.raises(ValidationError, match="element_type"):
        repo.upsert_chunks(
            tier=SearchTier.STABLE,
            chunks=[_chunk(ctx.tenant_id, element_type="spreadsheet-vibes")],
        )


# -- qdrant filter builder --------------------------------------------------


def test_the_filter_builder_requires_a_scope_key() -> None:
    """A filter without the tenant predicate is a cross-tenant read."""
    with pytest.raises(PermissionDeniedError, match="unscoped filter"):
        build_filter_spec("")


def test_the_filter_builder_rejects_a_malformed_scope_key() -> None:
    with pytest.raises(PermissionDeniedError, match="malformed"):
        build_filter_spec(":ws:user")


def test_the_tenant_predicate_is_always_present_and_first() -> None:
    """A filter without the tenant predicate is a cross-tenant read."""
    spec = build_filter_spec(f"tenant/{uuid4()}")

    assert spec.must[0].key == "tenant_id"
    assert UUID(spec.must[0].value)  # A concrete tenant UUID, never a wildcard.


def test_the_tenant_predicate_carries_the_tenant_segment_of_the_scope_key() -> None:
    tenant = uuid4()
    spec = build_filter_spec(f"tenant/{tenant}")

    assert spec.must[0].value == str(tenant)


def test_a_brandless_document_stays_visible_to_every_brand() -> None:
    spec = build_filter_spec(f"tenant/{uuid4()}", brand_ids=[uuid4()])

    # An OR including brand_id = null, so a brandless document is not filtered
    # out of a branded query.
    assert len(spec.should) == 2
    assert any(c.value is None for c in spec.should)


def test_no_channel_is_left_without_a_predicate() -> None:
    spec = build_filter_spec(
        f"tenant/{uuid4()}",
        kb_ids=[uuid4()],
        layers=["L3"],
        element_types=["table"],
        is_parent=False,
        min_updated_at=1,
    )

    assert spec.keys() == {
        "tenant_id",
        "kb_id",
        "layer",
        "element_type",
        "is_parent",
        "updated_at",
        "document_version_id",
        "doc_id",
        "representation_id",
        "index_id",
        "chunk_id",
        "representation_char_count",
        "parent_unit",
    }


def test_the_tenant_predicate_survives_every_optional_predicate() -> None:
    """Whatever else is filtered, tenant_id is still conjunctive."""
    spec = build_filter_spec(
        f"tenant/{uuid4()}",
        kb_ids=[uuid4()],
        brand_ids=[uuid4()],
        layers=["L1", "L2"],
        element_types=["text", "table"],
        is_parent=True,
        min_updated_at=99,
    )

    tenant_clauses = [c for c in spec.must if c.key == "tenant_id"]
    assert len(tenant_clauses) == 1
    assert "tenant_id" not in {c.key for c in spec.should}


def test_build_filter_adapts_the_spec_to_the_client_library() -> None:
    pytest.importorskip("qdrant_client")
    from qdrant_client.models import Filter

    qdrant_filter = build_filter(f"tenant/{uuid4()}", brand_ids=[uuid4()])

    assert isinstance(qdrant_filter, Filter)
    assert qdrant_filter.must is not None
    assert qdrant_filter.must[0].key == "tenant_id"
    assert len(qdrant_filter.should) == 2


# -- vector writer ----------------------------------------------------------


def test_vector_upsert_denies_a_foreign_tenant_chunk() -> None:
    pytest.importorskip("qdrant_client")
    from ragos.storage.vector import QdrantVectorIndexWriter

    ctx = _ctx()
    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=object(), embedding_dim=64)

    class _NoCollections:
        @staticmethod
        def get_collections():
            class _C:
                collections: list = []

            return _C()

    writer.client = _NoCollections()  # type: ignore[assignment]

    with pytest.raises(PermissionDeniedError, match="does not match"):
        writer.upsert(tenant_ctx=ctx, tier=_stable_tier(), chunks=[_chunk(uuid4())])


def test_collections_are_named_per_embedding_profile_not_per_tenant() -> None:
    """One collection per embedding model, never one per tenant."""
    from ragos.storage.vector import QdrantVectorIndexWriter

    writer = QdrantVectorIndexWriter(
        allow_test_embedding=True, client=object(), embedding_dim=64, embedding_profile="bge_m3"
    )
    name = writer.collection_name(_stable_tier())

    assert "bge_m3" in name
    assert str(uuid4()) not in name


def test_the_vision_collection_is_separate_from_the_text_collection() -> None:
    """Two embedding models in one collection make every distance meaningless."""
    from ragos.storage.vector import QdrantVectorIndexWriter

    writer = QdrantVectorIndexWriter(allow_test_embedding=True, client=object(), embedding_dim=64)
    text_collection = writer.collection_name(_stable_tier())
    vision_collection = writer.vision_collection_name("siglip_base")

    assert text_collection != vision_collection
    assert "vision" in vision_collection


def _stable_tier():
    from ragos.storage.repositories.search import SearchTier

    return SearchTier.STABLE
