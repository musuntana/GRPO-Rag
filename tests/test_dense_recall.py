"""Dense recall: ids from Qdrant, text from PostgreSQL.

Architecture §6.3. Two claims carry the design: a vector read is tenant-scoped by
the shared filter builder, and a hit is never delivered without its body. The
second is the one that quietly breaks answers — a title-only chunk with a
citation pointing at it is a confident, unverifiable sentence.

The vector-store outage case is asserted too: it raises rather than returning
nothing, because zero hits are indistinguishable from "the corpus does not
contain this".
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ProviderError
from ragos.core.tenant import TenantContext
from ragos.retrieval.gateway import GatewayEmbedder
from ragos.retrieval.recall.dense import DenseRecall
from ragos.retrieval.types import RetrieverProfile
from ragos.runtime.vector import build_filter_spec
from ragos.storage.repositories.search import SearchHit, SearchTier

TENANT_ID = uuid4()


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-dense",
    )


def _profile() -> RetrieverProfile:
    return RetrieverProfile(
        id=uuid4(),
        tenant_id=TENANT_ID,
        recall_channels=["dense"],
        channel_budgets={"dense": 1.0},
        top_k_recall=5,
    )


def _point(chunk_id: UUID, score: float, **payload: Any) -> Any:
    base = {
        "chunk_id": str(chunk_id),
        "doc_id": str(uuid4()),
        "kb_id": None,
        "layer": "L1",
        "parent_chunk_id": None,
        "page_no": 4,
        "page_count": 12,
        "start_char_idx": 100,
        "end_char_idx": 400,
        "element_type": "text",
    }
    base.update(payload)
    return type("Point", (), {"payload": base, "score": score})()


def _response(points: list[Any]) -> Any:
    return type("Response", (), {"points": points})()


class _FakeQdrant:
    def __init__(self, points: list[Any] | None = None, error: Exception | None = None) -> None:
        self.points = points or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def query_points(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _response(self.points)


class _FakeSource:
    """Stands in for ``SearchRepository`` with the two methods dense recall uses."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict[str, Any]] = []

    def get_chunks_by_ids(self, *, tier: SearchTier, scope_key: str, chunk_ids: list[UUID]):
        self.calls.append({"tier": tier, "scope_key": scope_key, "chunk_ids": chunk_ids})
        return [hit for hit in self.hits if hit.chunk_id in set(chunk_ids)]


def _hit(chunk_id: UUID, body: str, **overrides) -> SearchHit:
    fields = {
        "chunk_id": chunk_id,
        "document_id": uuid4(),
        "kb_id": None,
        "layer": "L1",
        "body": body,
        "metadata_json": {},
        "rank": 0.0,
        "page_no": 4,
        "page_count": 12,
        "start_char_idx": 100,
        "end_char_idx": 400,
        "element_type": "text",
    }
    fields.update(overrides)
    return SearchHit(**fields)


def _channel(client: Any, source: Any, recorder: list | None = None) -> DenseRecall:
    """A dense channel whose filter is the pure spec, not the client-library model.

    The tenant predicate is the security-relevant part and it lives in
    ``build_filter_spec``; asserting it through ``qdrant_client.models`` would
    make the guarantee untestable wherever the optional driver is absent.
    """
    return DenseRecall(
        client,
        "ragos_bge_m3_stable",
        _embedder(recorder if recorder is not None else []),
        source=source,
        filter_builder=build_filter_spec,
    )


def _embedder(recorder: list):
    async def embed(query: str, tenant_ctx: TenantContext) -> list[float]:
        recorder.append((query, tenant_ctx))
        return [0.1, 0.2, 0.3]

    return embed


# -- hydration --------------------------------------------------------------


def test_the_query_vector_is_metered_to_the_tenant() -> None:
    recorder: list = []
    channel = _channel(_FakeQdrant(), _FakeSource(), recorder)
    ctx = _ctx()

    asyncio.run(channel.recall("revenue?", ctx, _profile()))

    assert recorder[0][0] == "revenue?"
    assert recorder[0][1] is ctx


def test_chunk_text_comes_from_the_search_schema_not_the_payload() -> None:
    chunk_id = uuid4()
    source = _FakeSource([_hit(chunk_id, "Q2 revenue was $4M.")])
    channel = _channel(_FakeQdrant([_point(chunk_id, 0.87)]), source)

    hits = asyncio.run(channel.recall("revenue?", _ctx(), _profile()))

    assert [h.text for h in hits] == ["Q2 revenue was $4M."]
    assert hits[0].score == pytest.approx(0.87)
    assert source.calls[0]["chunk_ids"] == [chunk_id]
    assert source.calls[0]["tier"] == SearchTier.STABLE


def test_a_vector_without_a_search_row_is_dropped_not_delivered_empty() -> None:
    """Delivering a title-only chunk would put an unverifiable claim in the answer."""
    present, orphan = uuid4(), uuid4()
    source = _FakeSource([_hit(present, "real body")])
    channel = _channel(_FakeQdrant([_point(present, 0.9), _point(orphan, 0.8)]), source)

    hits = asyncio.run(channel.recall("q", _ctx(), _profile()))

    assert [h.chunk_id for h in hits] == [present]


def test_qdrants_ranking_survives_hydration_order() -> None:
    low, high = uuid4(), uuid4()
    source = _FakeSource([_hit(low, "low"), _hit(high, "high")])
    channel = _channel(_FakeQdrant([_point(low, 0.2), _point(high, 0.9)]), source)

    hits = asyncio.run(channel.recall("q", _ctx(), _profile()))

    assert [h.score for h in hits] == [0.2, 0.9]


def test_a_vector_store_outage_raises_r_than_returning_nothing() -> None:
    channel = _channel(_FakeQdrant(error=RuntimeError("connection refused")), _FakeSource())

    with pytest.raises(ProviderError, match="dense retrieval is unavailable"):
        asyncio.run(channel.recall("q", _ctx(), _profile()))


# -- scope ------------------------------------------------------------------


def test_the_read_filter_carries_the_tenant_predicate() -> None:
    client = _FakeQdrant()
    channel = _channel(client, _FakeSource())
    ctx = _ctx()
    ctx.brand_id = uuid4()
    ctx.allowed_kb_ids = [uuid4()]

    asyncio.run(channel.recall("q", ctx, _profile()))

    spec = client.calls[0]["query_filter"]
    assert [c.key for c in spec.must][0] == "tenant_id"
    assert "kb_id" in {c.key for c in spec.must}
    # A brand predicate is a disjunction that also admits brandless documents.
    assert [c.key for c in spec.should] == ["brand_id", "brand_id"]


def test_recall_without_a_scope_key_is_refused() -> None:
    channel = _channel(_FakeQdrant(), _FakeSource())
    ctx = _ctx()
    ctx.scope_key = ""

    with pytest.raises(PermissionDeniedError, match="scope_key"):
        asyncio.run(channel.recall("q", ctx, _profile()))


def test_the_recall_depth_comes_from_the_profile() -> None:
    client = _FakeQdrant()
    channel = _channel(client, _FakeSource())

    asyncio.run(channel.recall("q", _ctx(), _profile()))

    assert client.calls[0]["limit"] == 5
    assert client.calls[0]["with_payload"] is True


# -- gateway embedder -------------------------------------------------------


class _FakeRouter:
    def __init__(self, embeddings: list[list[float]] | None = None) -> None:
        self.embeddings = embeddings if embeddings is not None else [[0.5, 0.5]]
        self.calls: list[dict[str, Any]] = []

    def embed(self, tenant_ctx: TenantContext, *, texts: list[str], use_case: str = "embed"):
        self.calls.append({"tenant_ctx": tenant_ctx, "texts": texts, "use_case": use_case})
        return type(
            "Result", (), {"embeddings": self.embeddings, "model": "m", "usage": {}}
        )()


def test_the_embedder_goes_through_the_gateway_with_the_tenant() -> None:
    router = _FakeRouter()
    ctx = _ctx()

    vector = asyncio.run(GatewayEmbedder(router)("q", ctx))

    assert vector == [0.5, 0.5]
    assert router.calls[0]["tenant_ctx"] is ctx
    assert router.calls[0]["texts"] == ["q"]


def test_an_empty_embedding_response_is_an_error_not_a_zero_vector() -> None:
    """A zero vector lands at an arbitrary point and returns confident nonsense."""
    router = _FakeRouter(embeddings=[])

    with pytest.raises(ProviderError, match="no vector"):
        asyncio.run(GatewayEmbedder(router)("q", _ctx()))
