"""Character input budget rejects before dispatch; not a model token budget."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.gateway import GatewayGenerator
from ragos.retrieval.types import ScoredChunk


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_character_budget_cannot_create_generator(budget):
    with pytest.raises(ValidationError):
        GatewayGenerator(object(), max_evidence_chars=budget)


def test_total_evidence_over_budget_refused_before_provider_without_mutating_markers():
    class Router:
        def chat(self, *args, **kwargs):
            pytest.fail("over-budget evidence must not dispatch")

    chunks = [ScoredChunk(uuid4(), None, None, "abc", 1.0, "test") for _ in range(2)]
    ids = [chunk.chunk_id for chunk in chunks]
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="actor", trace_id="t")
    with pytest.raises(ValidationError, match="exceeds"):
        asyncio.run(GatewayGenerator(Router(), max_evidence_chars=5)("q", chunks, ctx))
    assert [chunk.chunk_id for chunk in chunks] == ids
    assert [chunk.text for chunk in chunks] == ["abc", "abc"]


def test_exact_character_budget_dispatches_without_dropping_evidence():
    calls = []

    class Router:
        def chat(self, tenant_ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="answer [1]")

    chunks = [ScoredChunk(uuid4(), None, None, "abc", 1.0, "test")]
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="test", actor_id="actor", trace_id="t")
    assert (
        asyncio.run(GatewayGenerator(Router(), max_evidence_chars=3)("q", chunks, ctx))
        == "answer [1]"
    )
    assert len(calls) == 1
    assert "abc" in calls[0]["messages"][-1]["content"]
