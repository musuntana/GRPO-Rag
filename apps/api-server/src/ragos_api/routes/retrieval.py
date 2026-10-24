"""``POST /tenant/retrieval/query`` — run the read path end to end.

This is the endpoint that makes the retrieval chain reachable: profile resolution,
query planning, multi-channel recall, fusion, rerank, the relevance gate, the
shared refine node, generation, and citation verification, with the explain
attached to the response.

The request cannot widen its own scope. Knowledge bases come from
``tenant_ctx.allowed_kb_ids`` and the profile is resolved inside the tenant's own
rows; a body that names a tenant, brand, kb, or ``scope_key`` is refused
(ADR-0005).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from ragos.auth.deps import require_tenant_admin
from ragos.core.tenant import TenantContext
from ragos.model_gateway.router import ModelRouter
from ragos.runtime.retrieval import (
    RetrievalOutcome,
    RetrievalService,
    build_parent_resolver,
    build_recall_channels,
)
from sqlmodel import Session

from ragos_api.dependencies import DBSession, TenantCtx
from ragos_api.scope import discard_scope_fields, enforce_body_scope

router = APIRouter(
    prefix="/tenant/retrieval",
    tags=["retrieval"],
    dependencies=[Depends(require_tenant_admin), Depends(enforce_body_scope)],
)


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    retriever_profile_id: UUID
    #: Pin an exact profile version. A live read takes the latest; an eval replay
    #: or a metric audit needs the version the number was produced under.
    retriever_profile_version: int | None = Field(default=None, ge=1)
    conversation_history: list[str] | None = Field(default=None, max_length=50)


def build_service(session: Session, tenant_ctx: TenantContext) -> RetrievalService:
    """Assemble the retrieval service for one request.

    All model traffic goes through one gateway instance, so a single request's
    generation, grading, query planning, and query embedding resolve against the
    same key pool and the same cost ledger.
    """
    router = ModelRouter(session)
    return RetrievalService(
        session=session,
        tenant_ctx=tenant_ctx,
        recall_channels=build_recall_channels(
            session=session, tenant_ctx=tenant_ctx, router=router
        ),
        parent_resolver=build_parent_resolver(session=session, tenant_ctx=tenant_ctx),
        router=router,
    )


def _service(session: DBSession, tenant_ctx: TenantCtx) -> RetrievalService:
    return build_service(session, tenant_ctx)


def serialize(outcome: RetrievalOutcome) -> dict[str, Any]:
    result = outcome.result
    explain = result.retrieval_explain
    return {
        "profile": {
            "profile_id": str(outcome.profile.id),
            "version": outcome.profile.version,
            "name": outcome.profile_name,
        },
        "answer": result.answer,
        "abstained": result.abstained,
        "citations": [c.to_dict() for c in result.citations],
        "gate": (
            {
                "verdict": result.gate.verdict,
                "confidence": result.gate.confidence,
                "action": result.gate.action,
                "rails_prompt_id": result.gate.prompt_id,
            }
            if result.gate
            else None
        ),
        "evidence": [
            {
                "chunk_id": str(c.chunk_id),
                "doc_id": str(c.doc_id) if c.doc_id else None,
                "kb_id": str(c.kb_id) if c.kb_id else None,
                "page_no": c.page_no,
                "score": c.score,
                "channel": c.channel,
            }
            for c in result.evidence
        ],
        "explain": {
            "query_strategy": explain.query_strategy,
            "rewritten_query": explain.rewritten_query,
            "refined_query": explain.refined_query,
            "recall_queries": explain.recall_queries,
            "sub_queries": explain.sub_queries,
            "used_history": explain.used_history,
            "plan_notes": explain.plan_notes,
            "per_channel_counts": explain.per_channel_counts,
            "channel_budgets": explain.channel_budgets,
            "fusion_scores": explain.fusion_scores,
            "rerank_scores": explain.rerank_scores,
            "re_retrieval_fusion_scores": explain.re_retrieval_fusion_scores,
            "re_retrieval_channel_counts": explain.re_retrieval_channel_counts,
            "rerank_depth": explain.rerank_depth,
            "rerank_model_profile": explain.rerank_model_profile,
            "gate_verdict": explain.gate_verdict,
            "gate_confidence": explain.gate_confidence,
            "gate_action": explain.gate_action,
            "rails_prompt_id": explain.rails_prompt_id,
            "re_retrieval_attempted": explain.re_retrieval_attempted,
            "re_retrieval_added_chunks": explain.re_retrieval_added_chunks,
            "parents_resolved": explain.parents_resolved,
            "citation_supported": explain.citation_supported,
            "citation_generated": explain.citation_generated,
        },
        "warnings": outcome.warnings,
    }


@router.post("/query")
async def run_query(
    payload: RetrievalRequest,
    session: DBSession,
    tenant_ctx: TenantCtx,
) -> dict[str, Any]:
    discard_scope_fields(payload.model_dump(), tenant_ctx, where="retrieval.query")

    service = _service(session, tenant_ctx)
    outcome = await service.query(
        query=payload.query,
        profile_id=payload.retriever_profile_id,
        profile_version=payload.retriever_profile_version,
        conversation_history=payload.conversation_history,
    )
    return serialize(outcome)
