"""SFT reranker baseline (ADR-0007 stage 2) — the deliverable.

A distilled cross-encoder trained on the mined pairs. It is what ships when
training runs at all: the GRPO stage exists to be measured against it, and
adopting the expensive method before the cheap measurable one would invert the
priorities (ADR-0004's reasoning applied to training).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

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


def build_examples(
    pairs: Sequence[MiningPair],
    text_by_doc: dict[str, str],
    config: SFTConfig | None = None,
) -> list[TrainingExample]:
    """Materialize mined pairs into weighted training examples.

    Grade 1 and grade 0 negatives are built into the same list but keep their
    own weight and their own grade, so a report can always recover the mix.
    """
    config = config or SFTConfig()
    config.validate()

    related, irrelevant = split_by_grade(list(pairs))
    examples: list[TrainingExample] = []

    for pair in related:
        pos = text_by_doc.get(pair.positive_doc_id)
        neg = text_by_doc.get(pair.negative_doc_id)
        if not pos or not neg:
            continue
        examples.append(
            TrainingExample(
                query=pair.question,
                positive_text=pos,
                negative_text=neg,
                negative_grade=pair.negative_grade,
                weight=config.related_weight,
            )
        )

    for pair in irrelevant:
        pos = text_by_doc.get(pair.positive_doc_id)
        neg = text_by_doc.get(pair.negative_doc_id)
        if not pos or not neg:
            continue
        examples.append(
            TrainingExample(
                query=pair.question,
                positive_text=pos,
                negative_text=neg,
                negative_grade=pair.negative_grade,
                weight=config.irrelevant_weight,
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
    import hashlib

    h = hashlib.sha256()
    for example in sorted(
        examples, key=lambda e: (e.query, e.positive_text, e.negative_text)
    ):
        h.update(example.query.encode("utf-8"))
        h.update(b"\x00")
        h.update(example.positive_text.encode("utf-8"))
        h.update(b"\x00")
        h.update(example.negative_text.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(example.negative_grade).encode("utf-8"))
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
    text_by_doc: dict[str, str],
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