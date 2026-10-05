"""Shared retrieval types.

Page anchoring (architecture §7.4.1) and the profile fields the v1.1 rules
require live here. ``RetrieverProfile`` carries the fields the design mandates
— ``join_mode``, per-channel budgets, explicit CRAG thresholds — rather than
the stale subset an earlier draft of ``implementation.md`` showed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from ragos.core.errors import ValidationError

ParentUnit = Literal["page", "token_window"]
JoinMode = Literal["concatenate", "reciprocal_rank_fusion", "distribution_based"]
QueryStrategy = Literal["direct", "hyde", "decompose"]


class ElementType(StrEnum):
    TEXT = "text"
    HEADING = "heading"
    TABLE = "table"
    FIGURE = "figure"
    LIST = "list"
    CODE = "code"
    CAPTION = "caption"


#: Canonical per-channel token budgets (architecture §8.3.1). Per profile, not
#: global: a tenant with a large structured corpus wants more lexical weight
#: than a conversational one.
DEFAULT_CHANNEL_BUDGETS: dict[str, float] = {
    "lexical": 0.20,
    "dense": 0.45,
    "memory": 0.15,
    "hot": 0.20,
}


@dataclass(slots=True)
class ScoredChunk:
    chunk_id: UUID
    doc_id: UUID | None
    kb_id: UUID | None
    text: str
    score: float
    channel: str
    layer: str = "L1"

    parent_chunk_id: UUID | None = None
    page_no: int | None = None
    page_count: int | None = None
    start_char_idx: int | None = None
    end_char_idx: int | None = None
    element_type: str = ElementType.TEXT.value
    rerank_score: float | None = None


@dataclass(slots=True)
class FusionConfig:
    join_mode: JoinMode = "reciprocal_rank_fusion"
    rrf_k: int = 60
    layer_boost: dict[str, float] = field(
        default_factory=lambda: {"L3": 1.5, "L2": 1.0, "L1": 0.8}
    )
    freshness_decay: float = 0.0
    authority_boost: float = 0.0


@dataclass(slots=True)
class CragThresholds:
    """Explicit, versioned CRAG thresholds (architecture §8.4)."""

    relevant_at: float = 0.7
    irrelevant_below: float = 0.3

    def validate(self) -> None:
        if self.relevant_at <= self.irrelevant_below:
            raise ValidationError(
                "crag thresholds overlap; relevant_at must exceed irrelevant_below",
                relevant_at=self.relevant_at,
                irrelevant_below=self.irrelevant_below,
            )


@dataclass(slots=True)
class RetrieverProfile:
    id: UUID
    tenant_id: UUID
    version: int = 1
    query_strategy: QueryStrategy = "direct"
    recall_channels: list[str] = field(
        default_factory=lambda: ["lexical", "dense", "hot"]
    )
    join_mode: JoinMode = "reciprocal_rank_fusion"
    channel_budgets: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_CHANNEL_BUDGETS)
    )
    fusion_config: FusionConfig = field(default_factory=FusionConfig)
    crag_thresholds: CragThresholds = field(default_factory=CragThresholds)
    crag_enabled: bool = True
    rerank_model_profile: str | None = None
    #: Semantic citation verification costs an LLM call per claim, so online it
    #: is opt-in per profile. Structural verification is always on.
    citation_verify_semantic: bool = False
    top_k_recall: int = 50
    top_k_rerank: int = 20
    top_k_generate: int = 6

    def validate(self) -> None:
        """Assert invariants at load time rather than trusting the stored row.

        A sum above 1.0 means the channels collectively promise more context
        than the budget allows, and the excess goes to whichever channel
        happened to return the largest raw scores.
        """
        unknown = [c for c in self.recall_channels if c not in self.channel_budgets]
        if unknown:
            raise ValidationError(
                "recall channel has no budget allocated", channels=unknown
            )
        total = sum(self.channel_budgets.values())
        if total > 1.0 + 1e-9:
            raise ValidationError(
                "per-channel token budgets must sum to <= 1.0", total=total
            )
        if self.top_k_rerank > self.top_k_recall:
            raise ValidationError(
                "top_k_rerank cannot exceed top_k_recall",
                top_k_recall=self.top_k_recall,
                top_k_rerank=self.top_k_rerank,
            )
        self.crag_thresholds.validate()


@dataclass(slots=True)
class GoldEvidence:
    """One graded gold document for an eval case (architecture §13.3.0)."""

    doc_id: UUID | str
    grade: int
    page_no: int | None = None
    chunk_id: UUID | None = None
    span: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.grade not in (0, 1, 2, 3):
            raise ValidationError(
                "relevance grade must be 0-3", grade=self.grade, doc_id=str(self.doc_id)
            )


@dataclass(slots=True)
class EvalCase:
    """Grounded evaluation case.

    ``split`` discipline is load-bearing: ``dev`` drives thresholds and
    hyperparameters, ``holdout`` decides promotion. Tuning on holdout turns the
    metric into a report of the tuning.
    """

    id: UUID
    tenant_id: UUID
    question: str
    kb_id: UUID | None = None
    gold_evidence: list[GoldEvidence] = field(default_factory=list)
    expected_answer: str | None = None
    answerable: bool = True
    supported_claims: list[str] = field(default_factory=list)
    review_state: str = "pending"
    labeler: str | None = None
    split: str = "dev"

    def answer_bearing(self) -> list[GoldEvidence]:
        return [g for g in self.gold_evidence if g.grade >= 3]


@dataclass(slots=True)
class ExperimentBundle:
    """Frozen version snapshots — not mutable profile ids.

    A mutable id would make a stored eval result unreproducible the moment the
    live profile changed. ``rails_prompt_version`` is included because the
    rails grader governs both production and evaluation.
    """

    prompt_version: str
    workflow_version: str
    model_profile_version: str
    retriever_profile_version: str
    parser_version: str
    chunker_version: str
    memory_profile_version: str
    rails_prompt_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "workflow_version": self.workflow_version,
            "model_profile_version": self.model_profile_version,
            "retriever_profile_version": self.retriever_profile_version,
            "parser_version": self.parser_version,
            "chunker_version": self.chunker_version,
            "memory_profile_version": self.memory_profile_version,
            "rails_prompt_version": self.rails_prompt_version,
        }

    @property
    def bundle_id(self) -> str:
        import hashlib

        blob = "|".join(f"{k}={v}" for k, v in sorted(self.as_dict().items()))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]