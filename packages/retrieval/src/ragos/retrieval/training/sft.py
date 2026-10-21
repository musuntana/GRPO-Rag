"""SFT reranker baseline (ADR-0007 stage 2) — the deliverable.

A distilled cross-encoder trained on the mined pairs. It is what ships when
training runs at all: the GRPO stage exists to be measured against it, and
adopting the expensive method before the cheap measurable one would invert the
priorities (ADR-0004's reasoning applied to training).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ragos.core.errors import ValidationError
from ragos.retrieval.training.mine import MiningPair, split_by_grade


@dataclass(slots=True)
class TrainingExample:
    """One pair with its label.

    ``weight`` differs by negative grade. Grade 1 negatives are the valuable
    ones, so down-weighting them equally with irrelevant ones throws away the
    signal the miner exists to find.
    """

    query: str
    positive_text: str
    negative_text: str
    negative_grade: int
    weight: float = 1.0
    case_id: str | None = None
    positive_doc_id: str | None = None
    positive_page_no: int | None = None
    negative_doc_id: str | None = None
    negative_page_no: int | None = None

    def label(self) -> int:
        return 1 if self.weight >= 0.0 else 0


@dataclass(slots=True)
class SFTConfig:
    epochs: int = 3
    batch_size: int = 16
    learning_rate: float = 2e-5
    #: Grade 1 negatives are weighted above grade 0. Not a free parameter —
    #: setting it to 1.0 collapses the miner back into a binary sampler.
    related_weight: float = 1.0
    irrelevant_weight: float = 0.4
    max_grad_norm: float = 1.0

    def validate(self) -> None:
        if self.epochs <= 0 or self.batch_size <= 0:
            raise ValueError("epochs and batch_size must be positive")
        if self.related_weight <= 0.0:
            raise ValueError("related_weight must be positive")


# A paginated corpus MUST use (document_id, page_no) as its text key.
# Bare document-id keys are supported only for genuinely unpaged examples.
TextLookup = Mapping[str | tuple[str, int | None], str]


def _require_evidence_text(
    lookup: TextLookup, doc_id: str, page_no: int | None, case_id: str
) -> str:
    text = lookup.get((doc_id, page_no))
    if text is None and page_no is None:
        text = lookup.get(doc_id)
    if not isinstance(text, str) or not text.strip():
        raise ValidationError(
            "training evidence text missing for document/page",
            case_id=case_id,
            doc_id=doc_id,
            page_no=page_no,
        )
    return text


def build_examples(
    pairs: Sequence[MiningPair],
    text_by_doc: TextLookup,
    config: SFTConfig | None = None,
) -> list[TrainingExample]:
    """Build weighted preference pairs using exact evidence-page identity.

    Never substitute a PDF-wide text blob for a page: that silently teaches
    the model identical text for positive and same-PDF hard-negative pages.
    Missing text fails closed instead of silently shrinking the training set.
    """
    config = config or SFTConfig()
    config.validate()
    related, irrelevant = split_by_grade(list(pairs))
    examples: list[TrainingExample] = []

    for pair in [*related, *irrelevant]:
        case_id = str(pair.case_id)
        pos = _require_evidence_text(
            text_by_doc, pair.positive_doc_id, pair.positive_page_no, case_id
        )
        neg = _require_evidence_text(
            text_by_doc, pair.negative_doc_id, pair.negative_page_no, case_id
        )
        if (
            pair.positive_doc_id == pair.negative_doc_id
            and pair.positive_page_no == pair.negative_page_no
        ):
            raise ValidationError("positive and negative evidence identity is identical")
        examples.append(
            TrainingExample(
                query=pair.question,
                positive_text=pos,
                negative_text=neg,
                negative_grade=pair.negative_grade,
                weight=(
                    config.related_weight
                    if pair.negative_grade == 1
                    else config.irrelevant_weight
                ),
                case_id=case_id,
                positive_doc_id=pair.positive_doc_id,
                positive_page_no=pair.positive_page_no,
                negative_doc_id=pair.negative_doc_id,
                negative_page_no=pair.negative_page_no,
            )
        )

    return examples


@dataclass(slots=True)
class TrainingRun:
    """Record of one offline training run.

    ``dataset_digest`` is what makes "it improved the set it trained on"
    checkable: same digest on both sides means the metric is not evidence.
    """

    tenant_scope_key: str
    method: str
    dataset_digest: str
    example_count: int
    related_count: int
    metrics: dict[str, float] = field(default_factory=dict)
    artifact_ref: str | None = None


def dataset_digest(examples: Sequence[TrainingExample]) -> str:
    """Stable hash of source identity, page text, labels and training weights.

    Without page IDs two different PDF pages with identical text can produce
    an identical digest; without weights different training mixes look equal.
    """
    import hashlib
    import json

    rows = [
        json.dumps(
            {
                "case_id": example.case_id,
                "query": example.query,
                "positive": {
                    "doc_id": example.positive_doc_id,
                    "page_no": example.positive_page_no,
                    "text": example.positive_text,
                },
                "negative": {
                    "doc_id": example.negative_doc_id,
                    "page_no": example.negative_page_no,
                    "text": example.negative_text,
                    "grade": example.negative_grade,
                },
                "weight": example.weight,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for example in examples
    ]
    h = hashlib.sha256()
    for row in sorted(rows):
        h.update(row.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:32]


def summarize(examples: Sequence[TrainingExample]) -> dict[str, float | int]:
    related = sum(1 for e in examples if e.negative_grade == 1)
    return {
        "examples": len(examples),
        "related": related,
        "irrelevant": len(examples) - related,
        "related_ratio": (related / len(examples)) if examples else 0.0,
    }


def train(
    pairs: Sequence[MiningPair],
    text_by_doc: TextLookup,
    *,
    tenant_scope_key: str,
    config: SFTConfig | None = None,
) -> TrainingRun:
    """Build the training set and return the run record.

    The gradient step itself belongs to whatever training runtime the deployment
    uses; what this package owns is the dataset construction and the record,
    because those are the parts that determine whether the run is meaningful.
    """
    config = config or SFTConfig()
    examples = build_examples(pairs, text_by_doc, config)
    stats = summarize(examples)
    return TrainingRun(
        tenant_scope_key=tenant_scope_key,
        method="sft",
        dataset_digest=dataset_digest(examples),
        example_count=int(stats["examples"]),
        related_count=int(stats["related"]),
    )