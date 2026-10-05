"""Retrieval pipeline: recall, fusion, rerank, relevance gate, citation verification.

Page-anchored hierarchy (architecture §7.4.1, ADR-0006) and per-tenant offline
reranker training (ADR-0007) live here alongside the recall/rerank division of
labour the pipeline is built on.
"""

from ragos.retrieval.citation import Citation, CitationStatus, EvidenceSet, bind_markers
from ragos.retrieval.grader import GateAction, GateResult, GateVerdict, RelevanceGrader
from ragos.retrieval.metrics import (
    ComparisonVerdict,
    RetrievalMetrics,
    abstention_accuracy,
    citation_accuracy,
    citation_completeness,
    compare_bundles,
    evaluate_retrieval,
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from ragos.retrieval.pipeline import RAGPipeline, RAGResult, RetrievalExplain
from ragos.retrieval.profile import latest_by_profile, profile_from_row
from ragos.retrieval.query import (
    ContextualRewriter,
    ConversationTurn,
    Decomposer,
    HyDE,
    QueryEngine,
    QueryPlan,
)
from ragos.retrieval.refine import RefineRequest, SentenceRefiner, deterministic_refine
from ragos.retrieval.types import (
    CragThresholds,
    EvalCase,
    ExperimentBundle,
    FusionConfig,
    GoldEvidence,
    RetrieverProfile,
    ScoredChunk,
)

__all__ = [
    "Citation",
    "CitationStatus",
    "ContextualRewriter",
    "ConversationTurn",
    "ComparisonVerdict",
    "CragThresholds",
    "Decomposer",
    "EvidenceSet",
    "EvalCase",
    "ExperimentBundle",
    "FusionConfig",
    "GateAction",
    "GateResult",
    "GateVerdict",
    "GoldEvidence",
    "HyDE",
    "QueryEngine",
    "QueryPlan",
    "RAGPipeline",
    "RAGResult",
    "RefineRequest",
    "RelevanceGrader",
    "RetrievalExplain",
    "RetrievalMetrics",
    "RetrieverProfile",
    "ScoredChunk",
    "SentenceRefiner",
    "abstention_accuracy",
    "bind_markers",
    "citation_accuracy",
    "citation_completeness",
    "compare_bundles",
    "deterministic_refine",
    "evaluate_retrieval",
    "hit_at_k",
    "latest_by_profile",
    "mrr",
    "ndcg_at_k",
    "precision_at_k",
    "profile_from_row",
    "recall_at_k",
]
