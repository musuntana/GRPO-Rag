"""Authorized page trace capture with real PG and Qdrant + failure gates.

The CI service fixture uses deterministic test embeddings. No Qwen weights,
production tenant data, model budgets or quality gains are validated here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.offline_eval import evaluate_trace_bytes
from ragos.retrieval.types import EvalCase, GoldEvidence, RetrieverProfile, ScoredChunk
from ragos.runtime.page_trace_capture import capture_page_ranking_trace
from ragos.storage.repositories.search import SearchRepository, SearchTier
from test_frozen_qwen_embedding_probe import _binding
from test_pdf_pg_index_integration import _real_pdf_fixture
from test_qdrant_server_pg_reconnect import qdrant_url
from test_storage_integration import database, scoped_session

__all__ = ["database", "qdrant_url"]


def _case(plan, tenant_ctx, *, gold_page=1, split="holdout"):
    return EvalCase(
        id=uuid4(),
        tenant_id=tenant_ctx.tenant_id,
        kb_id=plan.kb_id,
        question="Contract warranty period is 24 months.",
        gold_evidence=[GoldEvidence(doc_id=plan.document_id, page_no=gold_page, grade=3)],
        split=split,
        answerable=True,
        review_state="approved",
    )


def _profile(tenant_ctx):
    return RetrieverProfile(
        id=uuid4(),
        tenant_id=tenant_ctx.tenant_id,
        recall_channels=["dense"],
        channel_budgets={"dense": 1.0},
        top_k_recall=50,
    )


def _capture(ctx, plan, dense, profile, cases, *, split="holdout"):
    return asyncio.run(
        capture_page_ranking_trace(
            tenant_ctx=ctx,
            plan=plan,
            dense=dense,
            profile=profile,
            cases=cases,
            split=split,
            dataset_id="synthetic-pdf-page-gold-v1",
            retriever_version="fixture-dense-baseline-v1",
        )
    )


def test_real_qdrant_postgres_produce_scored_page_trace(
    database, qdrant_url, tmp_path
) -> None:
    binding = _binding()
    fixture = _real_pdf_fixture(database, tmp_path, embedding=binding)
    fixture.client.close()
    writer = QdrantClient(url=qdrant_url, timeout=5)
    prefix = "ragos_trace_ci_" + uuid4().hex[:12]
    fixture.client = writer
    fixture.writer = binding.writer(writer, collection_prefix=prefix)
    collection = fixture.writer.collection_name(SearchTier.STABLE)
    reader = None
    try:
        # Staged vectors are not authoritative evidence until the SQL CAS.
        rows = fixture.plan.as_upserts(fixture.ctx)
        fixture.writer.upsert(
            tenant_ctx=fixture.ctx, tier=SearchTier.STABLE, chunks=rows
        )
        case = _case(fixture.plan, fixture.ctx)
        with scoped_session(database, fixture.ctx) as session:
            dense = binding.dense(
                writer, source=SearchRepository(session, fixture.ctx),
                plan=fixture.plan, collection_prefix=prefix,
            )
            staged = _capture(
                fixture.ctx, fixture.plan, dense, _profile(fixture.ctx), [case]
            )
            staged_report = evaluate_trace_bytes(staged)
            assert staged_report["metrics"]["evidence_hit_rate"] == 0.0
            assert staged_report["metrics"]["recall_at_50"] == 0.0

        result = fixture.service().execute(manifest_id=fixture.plan.index_id)
        assert result.indexing_state == "indexed"
        writer.close()
        reader = QdrantClient(url=qdrant_url, timeout=5)
        with scoped_session(database, fixture.ctx) as session:
            dense = binding.dense(
                reader, source=SearchRepository(session, fixture.ctx),
                plan=fixture.plan, collection_prefix=prefix,
            )
            captured = _capture(
                fixture.ctx, fixture.plan, dense, _profile(fixture.ctx), [case]
            )
        record = [json.loads(line) for line in captured.splitlines()]
        assert record[0]["index_id"] == str(fixture.plan.index_id)
        assert record[0]["embedding_version"] == fixture.plan.embedding_version
        assert record[1]["gold"] == [{
            "doc_id": str(fixture.plan.document_id), "page_no": 1, "grade": 3
        }]
        assert len(record[1]["retrieved"]) == 4
        assert {item["page_no"] for item in record[1]["retrieved"]} == {1, 2}
        assert "Contract warranty period is 24 months." in case.question
        assert "retained deposit is 5 percent" not in captured.decode()
        assert "document_version_id" not in record[1]  # no source-content leakage
        scored = evaluate_trace_bytes(captured)
        assert scored["case_total"] == 1
        assert scored["duplicate_ranked_pages_removed"] == 2
        assert scored["metrics"]["evidence_hit_rate"] == 1.0
        assert scored["metrics"]["recall_at_50"] == 1.0
        assert scored["evidence_kind"] == "offline_recorded_ranks_no_model_calls"

        # A wrong (but page-in-bounds) gold is not used to steer the ranking.
        wrong = replace(case, gold_evidence=[
            GoldEvidence(doc_id=fixture.plan.document_id, page_no=2, grade=3)
        ])
        with scoped_session(database, fixture.ctx) as session:
            dense = binding.dense(
                reader, source=SearchRepository(session, fixture.ctx),
                plan=fixture.plan, collection_prefix=prefix,
            )
            changed_gold = _capture(
                fixture.ctx, fixture.plan, dense, _profile(fixture.ctx), [wrong]
            )
        assert json.loads(changed_gold.splitlines()[1])["retrieved"] == record[1]["retrieved"]

        foreign = TenantContext(
            tenant_id=uuid4(),
            tenant_slug="foreign",
            actor_id="integration",
            trace_id="foreign",
            allowed_kb_ids=[fixture.plan.kb_id],
        )
        with pytest.raises(PermissionDeniedError, match="different tenant"):
            _capture(foreign, fixture.plan, dense, _profile(foreign), [case])
    finally:
        client = reader or QdrantClient(url=qdrant_url, timeout=5)
        try:
            if collection in {c.name for c in client.get_collections().collections}:
                client.delete_collection(collection_name=collection)
        finally:
            client.close()
            writer.close()


class _NeverCalledRetriever:
    def __init__(self, *, hits=None):
        self.count = 0
        self.hits = [] if hits is None else hits

    async def recall(self, query, tenant_ctx, profile):
        assert isinstance(query, str)  # no EvalCase/gold may enter the retriever
        self.count += 1
        return self.hits


def test_reject_invalid_gold_and_scope_before_retrieving() -> None:
    tenant_id, kb_id, doc_id, index_id = uuid4(), uuid4(), uuid4(), uuid4()
    ctx = TenantContext(
        tenant_id=tenant_id, tenant_slug="local", actor_id="fixture",
        trace_id="trace", allowed_kb_ids=[kb_id],
    )
    plan = SimpleNamespace(
        tenant_id=tenant_id, kb_id=kb_id, document_id=doc_id,
        index_id=index_id, page_count=2,
        embedding_profile="qwen3", embedding_version="frozen-v1",
    )
    retriever = _NeverCalledRetriever()
    case = _case(plan, ctx)
    with pytest.raises(ValidationError, match="approved"):
        _capture(
            ctx, plan, retriever, _profile(ctx),
            [replace(case, review_state="pending")],
        )
    with pytest.raises(ValidationError, match="outside frozen PDF"):
        _capture(
            ctx, plan, retriever, _profile(ctx),
            [replace(case, gold_evidence=[
                GoldEvidence(doc_id=doc_id, page_no=8, grade=3)
            ])],
        )
    with pytest.raises(ValidationError, match="duplicate human"):
        _capture(
            ctx, plan, retriever, _profile(ctx),
            [replace(case, gold_evidence=[
                GoldEvidence(doc_id=doc_id, page_no=1, grade=3),
                GoldEvidence(doc_id=doc_id, page_no=1, grade=1),
            ])],
        )
    with pytest.raises(ValidationError, match="unique"):
        _capture(ctx, plan, retriever, _profile(ctx), [case, case])
    with pytest.raises(PermissionDeniedError, match="authorized KB"):
        denied = ctx.model_copy(update={"allowed_kb_ids": None})
        _capture(denied, plan, retriever, _profile(denied), [case])
    assert retriever.count == 0


def test_reject_fake_hydrated_page_identity_instead_of_optimistic_scoring() -> None:
    tenant_id, kb_id, doc_id, index_id, version_id, repr_id = [uuid4() for _ in range(6)]
    ctx = TenantContext(
        tenant_id=tenant_id, tenant_slug="fixture", actor_id="server",
        trace_id="replay", allowed_kb_ids=[kb_id],
    )
    plan = SimpleNamespace(
        tenant_id=tenant_id, kb_id=kb_id, document_id=doc_id,
        document_version_id=version_id, representation_id=repr_id,
        index_id=index_id, page_count=2,
        embedding_profile="qwen3", embedding_version="frozen-v1",
    )
    fake = ScoredChunk(
        chunk_id=uuid4(), doc_id=doc_id, kb_id=kb_id,
        text="fabricated text", score=0.9, channel="dense",
        page_no=1, source_text="fabricated text",
    )
    retriever = _NeverCalledRetriever(hits=[fake])
    with pytest.raises(ValidationError, match="unversioned"):
        _capture(ctx, plan, retriever, _profile(ctx), [_case(plan, ctx)])
    assert retriever.count == 1


def test_approved_train_trace_is_separate_from_dev_and_holdout() -> None:
    tenant_id, kb_id, doc_id, index_id = uuid4(), uuid4(), uuid4(), uuid4()
    ctx = TenantContext(
        tenant_id=tenant_id, tenant_slug="train", actor_id="reviewer",
        trace_id="train-only-capture", allowed_kb_ids=[kb_id],
    )
    plan = SimpleNamespace(
        tenant_id=tenant_id, kb_id=kb_id, document_id=doc_id,
        index_id=index_id, page_count=2,
        embedding_profile="test_profile", embedding_version="frozen-version",
    )
    case = _case(plan, ctx, split="train")
    dense = _NeverCalledRetriever()
    raw = _capture(ctx, plan, dense, _profile(ctx), [case], split="train")
    manifest = json.loads(raw.splitlines()[0])
    assert manifest["schema_version"] == 2
    assert manifest["split"] == "train"
    assert evaluate_trace_bytes(raw)["trace_format"] == "page-ranking-trace-train-v2"
    assert dense.count == 1
    assert json.loads(raw.splitlines()[1])["retrieved"] == []
