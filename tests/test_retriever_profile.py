"""Stored retriever profile → runtime profile, and version immutability.

Architecture §8.3, §13.3: the profile is versioned configuration, and the row is
frozen so a recorded eval result still means something after the live profile
moves on.

The claims under test: an invalid stored configuration is refused rather than
repaired, budgetless rows resolve to the documented defaults, and the version
number is derived from storage rather than accepted from the caller.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from ragos.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.profile import latest_by_profile, profile_from_row
from ragos.storage.models.app import RetrieverProfileVersion
from ragos.storage.repositories.retriever import RetrieverProfileRepository

OTHER_TENANT = uuid4()


def _ctx(tenant_id: uuid4 | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid4(),
        tenant_slug="acme",
        actor_id="u_admin",
        trace_id="trace-1",
    )


def _row(tenant_id: uuid4, **overrides) -> RetrieverProfileVersion:
    fields = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "profile_id": uuid4(),
        "version": 1,
        "name": "default",
        "query_strategy": "direct",
        "recall_channels": ["lexical", "dense"],
        "join_mode": "reciprocal_rank_fusion",
        "channel_budgets": {"lexical": 0.2, "dense": 0.5},
        "fusion_config": {},
        "crag_thresholds": {},
        "crag_enabled": True,
        "rerank_model_profile": None,
        "citation_verify_semantic": False,
        "top_k_recall": 50,
        "top_k_rerank": 20,
        "top_k_generate": 6,
    }
    fields.update(overrides)
    return RetrieverProfileVersion(**fields)


# -- row → runtime profile --------------------------------------------------


def test_a_stored_row_becomes_a_validated_runtime_profile() -> None:
    tenant_id = uuid4()

    profile = profile_from_row(_row(tenant_id, query_strategy="hyde", crag_enabled=False))

    assert profile.query_strategy == "hyde"
    assert profile.crag_enabled is False
    assert profile.recall_channels == ["lexical", "dense"]
    assert profile.tenant_id == tenant_id


def test_an_empty_budget_column_resolves_to_the_documented_defaults() -> None:
    """A row written before the column existed means the defaults, not a zero budget."""
    profile = profile_from_row(
        _row(uuid4(), channel_budgets={}, recall_channels=["lexical", "dense", "memory", "hot"])
    )

    assert sum(profile.channel_budgets.values()) == pytest.approx(1.0)
    profile.validate()


def test_budgets_that_exceed_the_total_are_refused_not_repaired() -> None:
    """Silently rescaling would mean the profile on the eval record is not the one that ran."""
    with pytest.raises(ValidationError):
        profile_from_row(_row(uuid4(), channel_budgets={"lexical": 0.7, "dense": 0.7}))


def test_a_channel_with_no_budget_is_refused() -> None:
    with pytest.raises(ValidationError):
        profile_from_row(_row(uuid4(), channel_budgets={"lexical": 1.0}))


def test_overlapping_crag_thresholds_are_refused() -> None:
    with pytest.raises(ValidationError):
        profile_from_row(
            _row(uuid4(), crag_thresholds={"relevant_at": 0.2, "irrelevant_below": 0.5})
        )


def test_an_unknown_strategy_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ValidationError):
        profile_from_row(_row(uuid4(), query_strategy="agentic_v9"))


def test_a_deep_top_k_is_refused() -> None:
    with pytest.raises(ValidationError):
        profile_from_row(_row(uuid4(), top_k_rerank=80, top_k_recall=50))


def test_a_version_that_skips_a_number_is_refused_as_ambiguous_evidence() -> None:
    with pytest.raises(ValidationError):
        profile_from_row(_row(uuid4(), version=0))


def test_latest_by_profile_compares_versions_not_row_order() -> None:
    tenant_id = uuid4()
    profile_id = uuid4()
    rows = [
        _row(tenant_id, profile_id=profile_id, version=3),
        _row(tenant_id, profile_id=profile_id, version=1),
        _row(tenant_id, profile_id=profile_id, version=2),
    ]

    latest = latest_by_profile(rows)

    assert latest[profile_id].version == 3


# -- repository scope enforcement -------------------------------------------


class _Session:
    """Records statements; returns whatever rows a test staged."""

    def __init__(self, rows: list[RetrieverProfileVersion] | None = None) -> None:
        self.rows = rows or []
        self.added: list[RetrieverProfileVersion] = []
        self.flushed = 0
        self.refreshed = 0

    def exec(self, statement):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def first(self):
                return self._rows[0] if self._rows else None

        return _Result(self.rows)

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1

    def refresh(self, _row) -> None:
        self.refreshed += 1


def _repo(tenant_ctx: TenantContext, rows=None) -> tuple[RetrieverProfileRepository, _Session]:
    session = _Session(rows)
    return RetrieverProfileRepository(session=session, tenant_ctx=tenant_ctx), session


def test_the_repository_refuses_to_exist_without_a_scope_key() -> None:
    ctx = _ctx()
    ctx = ctx.model_copy(update={"tenant_id": None})

    with pytest.raises(PermissionDeniedError):
        RetrieverProfileRepository(session=_Session(), tenant_ctx=ctx)


@pytest.mark.parametrize(
    "method,args",
    [
        ("list_profiles", {}),
        ("list_versions", {"profile_id": uuid4()}),
        ("require_latest_version", {"profile_id": uuid4()}),
        ("require_version", {"profile_id": uuid4(), "version": 1}),
        ("create_version", {"profile_id": uuid4()}),
    ],
)
def test_every_read_and_write_demands_an_explicit_scope_key(method: str, args: dict) -> None:
    ctx = _ctx()
    repo, _ = _repo(ctx)

    with pytest.raises(PermissionDeniedError):
        getattr(repo, method)(scope_key="", **args)


def test_a_scope_key_from_another_tenant_is_refused() -> None:
    """The check that makes the required parameter meaningful rather than decorative."""
    ctx = _ctx()
    repo, _ = _repo(ctx)

    with pytest.raises(PermissionDeniedError):
        repo.list_profiles(scope_key=f"{OTHER_TENANT}::u_admin")


def test_a_missing_profile_is_a_not_found_not_an_empty_list() -> None:
    """An empty list reads as "this tenant has no profiles"; a 404 reads as "wrong id"."""
    ctx = _ctx()
    repo, _ = _repo(ctx, rows=[])

    with pytest.raises(NotFoundError):
        repo.require_latest_version(scope_key=ctx.scope_key, profile_id=uuid4())


def test_a_new_version_is_appended_rather_than_rewriting_the_old_one() -> None:
    ctx = _ctx()
    profile_id = uuid4()
    existing = [_row(ctx.tenant_id, profile_id=profile_id, version=4)]
    repo, session = _repo(ctx, rows=existing)

    created = repo.create_version(
        scope_key=ctx.scope_key,
        profile_id=profile_id,
        query_strategy="hyde",
        recall_channels=["lexical"],
        channel_budgets={"lexical": 1.0},
        join_mode="reciprocal_rank_fusion",
        fusion_config={},
        crag_thresholds={},
        crag_enabled=True,
        rerank_model_profile=None,
        citation_verify_semantic=False,
        top_k_recall=50,
        top_k_rerank=20,
        top_k_generate=6,
    )

    assert created.version == 5
    assert created.query_strategy == "hyde"
    assert existing[0].version == 4, "the previous version row must not be touched"
    assert len(session.added) == 1


def test_the_first_version_of_a_new_profile_is_one() -> None:
    ctx = _ctx()
    repo, _ = _repo(ctx, rows=[])

    created = repo.create_version(scope_key=ctx.scope_key, profile_id=uuid4())

    assert created.version == 1
    assert created.tenant_id == ctx.tenant_id


def test_versions_are_listed_newest_first() -> None:
    ctx = _ctx()
    profile_id = uuid4()
    rows = [_row(ctx.tenant_id, profile_id=profile_id, version=i) for i in (3, 1, 2)]
    repo, _ = _repo(ctx, rows=rows)

    listed = repo.list_versions(scope_key=ctx.scope_key, profile_id=profile_id)

    assert [row.version for row in listed] == [3, 1, 2]


# -- RetrievalService capability checks --------------------------------------


async def _recall(_query, _profile, _ctx):
    return []


def _service(ctx: TenantContext, **kwargs):
    from ragos.runtime.retrieval import RetrievalService

    kwargs.setdefault("recall_channels", {"lexical": _recall})
    return RetrievalService(session=_Session(), tenant_ctx=ctx, **kwargs)


async def _judge(_query, _messages, _ctx) -> str:
    return "{}"


async def _generator(_question, _evidence, _ctx) -> str:
    return "answer"


def _runtime_profile(**overrides):
    from ragos.retrieval.types import RetrieverProfile

    fields = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "recall_channels": ["lexical"],
        "channel_budgets": {"lexical": 1.0},
        "crag_enabled": False,
    }
    fields.update(overrides)
    return RetrieverProfile(**fields)


def test_a_profile_naming_an_unwired_channel_reports_it_rather_than_retrieving_silently() -> None:
    ctx = _ctx()
    service = _service(ctx, generator=_generator)

    _pipeline, warnings = service.build_pipeline(
        _runtime_profile(recall_channels=["lexical", "dense"])
    )

    assert warnings == ["recall channel 'dense' is configured but not wired in this deployment"]


def test_semantic_verification_without_an_entailment_callable_is_refused() -> None:
    service = _service(_ctx(), generator=_generator)

    with pytest.raises(ValidationError, match="semantic citation verification"):
        service.build_pipeline(_runtime_profile(citation_verify_semantic=True))


def test_an_enabled_gate_without_a_judge_is_refused() -> None:
    """Answering un-gated while the profile claims a gate is a silent downgrade of a
    stated guarantee."""
    service = _service(_ctx(), generator=_generator)

    with pytest.raises(ValidationError, match="relevance gate"):
        service.build_pipeline(_runtime_profile(crag_enabled=True))


def test_a_disabled_gate_needs_no_judge() -> None:
    service = _service(_ctx(), generator=_generator)

    _pipeline, warnings = service.build_pipeline(_runtime_profile(crag_enabled=False))

    assert warnings == []


def test_a_configured_reranker_without_a_callable_is_reported() -> None:
    service = _service(_ctx(), generator=_generator)

    _pipeline, warnings = service.build_pipeline(
        _runtime_profile(rerank_model_profile="bge-reranker-v2")
    )

    assert len(warnings) == 1 and "rerank" in warnings[0]


def test_a_profile_with_no_generator_is_refused() -> None:
    service = _service(_ctx())

    with pytest.raises(ValidationError, match="generator"):
        service.build_pipeline(_runtime_profile())


def test_the_query_path_demands_an_explicit_profile() -> None:
    import asyncio

    service = _service(_ctx(), generator=_generator)

    with pytest.raises(ValidationError, match="retriever_profile_id"):
        asyncio.run(service.query(query="q"))


# -- channel wiring ---------------------------------------------------------


def test_lexical_and_hot_are_always_wired() -> None:
    """Both are PostgreSQL-backed, so neither depends on an optional service."""
    from ragos.runtime.retrieval import build_recall_channels

    channels = build_recall_channels(
        session=_Session(), tenant_ctx=_ctx(), dense=False, router=None
    )

    assert set(channels) == {"lexical", "hot"}


def test_dense_is_wired_only_with_a_client_and_an_embedding_route() -> None:
    from ragos.core.config import Settings
    from ragos.runtime.retrieval import build_recall_channels

    without_client = build_recall_channels(session=_Session(), tenant_ctx=_ctx(), router=_Router())
    with_client = build_recall_channels(
        session=_Session(),
        tenant_ctx=_ctx(),
        router=_Router(),
        qdrant_client=object(),
        settings=Settings(app_env="test", task_test_embedding=True),
    )

    assert "dense" not in without_client
    assert "dense" in with_client


def test_the_dense_collection_is_named_per_embedding_profile_not_per_tenant() -> None:
    from ragos.core.config import Settings
    from ragos.runtime.retrieval import dense_collection_name

    a = dense_collection_name(Settings(embedding_profile="bge_m3"))
    b = dense_collection_name(Settings(embedding_profile="gte_large"))

    assert a.startswith("ragos_bge_m3_") and a.endswith("_stable")
    assert b != a
    assert "tenant" not in a


def _Router():
    class _Router:
        def chat(self, *_a, **_kw):  # pragma: no cover - not exercised here
            raise AssertionError

        def embed(self, *_a, **_kw):  # pragma: no cover - not exercised here
            raise AssertionError

    return _Router()


def test_dense_query_refuses_an_unfrozen_production_embedding():
    from ragos.core.config import Settings
    from ragos.core.errors import ValidationError
    from ragos.runtime.retrieval import build_recall_channels

    with pytest.raises(ValidationError, match="identity is not frozen"):
        build_recall_channels(
            session=_Session(),
            tenant_ctx=_ctx(),
            router=_Router(),
            qdrant_client=object(),
            settings=Settings(app_env="dev"),
        )


def test_dense_reader_and_writer_share_full_embedding_identity_namespace():
    from ragos.core.config import Settings
    from ragos.runtime.retrieval import dense_collection_name
    from ragos.storage.repositories.search import SearchTier
    from ragos.storage.vector import QdrantVectorIndexWriter

    cfg = Settings(embedding_profile="synthetic", embedding_version="version-one")
    writer = QdrantVectorIndexWriter(
        client=object(),
        allow_test_embedding=True,
        embedding_profile=cfg.embedding_profile,
        embedding_version=cfg.embedding_version,
    )
    assert dense_collection_name(cfg) == writer.collection_name(SearchTier.STABLE)
    assert dense_collection_name(cfg) != dense_collection_name(
        Settings(embedding_profile="synthetic", embedding_version="version-two")
    )


def test_disabled_dense_ignores_injected_client_and_router():
    from ragos.core.config import Settings
    from ragos.runtime.retrieval import build_recall_channels

    channels = build_recall_channels(
        session=_Session(),
        tenant_ctx=_ctx(),
        dense=False,
        router=_Router(),
        qdrant_client=object(),
        settings=Settings(app_env="dev"),
    )
    assert set(channels) == {"lexical", "hot"}
