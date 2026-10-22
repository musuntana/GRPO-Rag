"""Tenant-bound reranker dispatch and evidence-safe gateway conversion.

These tests use an injected fake provider. They do not call real Qwen weights.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ProviderError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.model_gateway.providers import RerankEntry, ResolvedRoute
from ragos.model_gateway.router import ModelRouter
from ragos.retrieval.gateway import GatewayRerankAdapter
from ragos.retrieval.types import ScoredChunk


def _ctx():
    return TenantContext(
        tenant_id=uuid4(), tenant_slug="test",
        actor_id="authorized", trace_id="rerank-contract",
    )


def _provider(results):
    calls = []

    def rerank(*, query, documents, model, parameters):
        calls.append((query, list(documents), model, parameters))
        return results

    return SimpleNamespace(rerank=rerank), calls


def _router(monkeypatch, provider, *, denied=False):
    router = ModelRouter.__new__(ModelRouter)

    def resolve(ctx, use_case, *, profile_override=None):
        assert use_case == "rerank"
        assert profile_override is None
        if denied:
            raise PermissionDeniedError("No authorized tenant rerank binding")
        return ResolvedRoute(
            provider=provider, provider_slug="fixture", model_name="fixture-rerank",
            parameters={}, profile_id=str(uuid4()),
        )

    monkeypatch.setattr(router, "resolve", resolve)
    return router


def _chunks():
    return [
        ScoredChunk(chunk_id=uuid4(), doc_id=None, kb_id=None,
                    text="warranty 24 months", score=0.1, channel="dense"),
        ScoredChunk(chunk_id=uuid4(), doc_id=None, kb_id=None,
                    text="invoice only", score=0.9, channel="dense"),
    ]


def test_tenant_router_reranks_and_adapter_keeps_original_source(monkeypatch):
    provider, calls = _provider([
        RerankEntry(index=1, score=0.95),
        RerankEntry(index=0, score=0.2),
    ])
    router = _router(monkeypatch, provider)
    chunks = _chunks()
    result = asyncio.run(GatewayRerankAdapter(router)("warranty?", chunks, _ctx()))
    assert [x.chunk_id for x in result] == [chunks[1].chunk_id, chunks[0].chunk_id]
    assert [x.score for x in result] == [0.95, 0.2]
    assert [x.text for x in result] == [chunks[1].text, chunks[0].text]
    assert [x.score for x in chunks] == [0.1, 0.9]
    assert calls == [("warranty?", [x.text for x in chunks], "fixture-rerank", {})]


@pytest.mark.parametrize("bad", [
    [RerankEntry(index=0, score=0.9)],
    [RerankEntry(index=0, score=0.9), RerankEntry(index=0, score=0.1)],
    [RerankEntry(index=0, score=float("nan")), RerankEntry(index=1, score=0.1)],
    [RerankEntry(index=0, score=True), RerankEntry(index=1, score=0.1)],
    [RerankEntry(index=0, score=10**1000), RerankEntry(index=1, score=0.1)],
    [RerankEntry(index=0, score=0.9), RerankEntry(index=3, score=0.1)],
])
def test_router_rejects_partial_duplicate_or_nonfinite_scores(monkeypatch, bad):
    provider, calls = _provider(bad)
    router = _router(monkeypatch, provider)
    with pytest.raises(ProviderError):
        router.rerank(_ctx(), query="warranty?", documents=["one", "two"])
    assert len(calls) == 1


def test_unauthorized_tenant_never_calls_provider(monkeypatch):
    provider, calls = _provider([
        RerankEntry(index=0, score=0.9),
    ])
    router = _router(monkeypatch, provider, denied=True)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(GatewayRerankAdapter(router)("warranty?", _chunks(), _ctx()))
    assert calls == []


def test_input_budget_denied_before_route_resolution(monkeypatch):
    provider, calls = _provider([])
    router = _router(monkeypatch, provider, denied=True)
    ctx = _ctx()
    with pytest.raises(ValidationError, match="bounded"):
        router.rerank(ctx, query="q", documents=["x" * 16001])
    with pytest.raises(ValidationError, match="bounded"):
        router.rerank(ctx, query="q", documents=["x"] * 51)
    with pytest.raises(ValidationError, match="budget"):
        asyncio.run(
            GatewayRerankAdapter(router, max_total_chars=2)(
                "q", _chunks(), ctx
            )
        )
    assert calls == []


def test_adapter_rejects_bad_router_response_without_mutating_evidence():
    class FaultyRouter:
        def rerank(self, *args, **kwargs):
            return [RerankEntry(index=0, score=0.8)]

    chunks = _chunks()
    with pytest.raises(ProviderError, match="incomplete"):
        asyncio.run(GatewayRerankAdapter(FaultyRouter())("query", chunks, _ctx()))
    assert [chunk.score for chunk in chunks] == [0.1, 0.9]
