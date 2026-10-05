"""Memory recall ranking, cheapest signal first.

Ordering is a cost ladder, not a preference (AGENTS.md memory rule 9):

    mention frequency  -> graph distance  -> RRF  -> MMR  -> cross-encoder

Each stage is either free or nearly free, and the expensive one runs last on a
handful of survivors. The alternative — leading with a cross-encoder over every
fact the user has — spends a model call per fact on every turn to re-derive a
ranking that mention counts usually already get right.

Two consequences worth stating:

- the cross-encoder stage is bounded by ``top_n``. An unbounded final stage turns
  the ladder back into "cross-encode everything", which is the cost defect the
  ladder exists to avoid.
- MMR runs before the cross-encoder because diversity is a cheap structural
  property. Spending a model call to discover that five near-identical facts were
  the top five is paying twice for one insight.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ragos.memory.types import MemoryFact

#: RRF constant. Same convention as the retrieval fusion layer so the two ladders
#: produce comparable scores.
RRF_K = 60


@dataclass(slots=True)
class RankedFact:
    fact: MemoryFact
    score: float
    stage: str


#: Signature of the final cross-encoder stage. Only ever called with the bounded
#: survivor set.
CrossEncoderFn = Callable[[str, Sequence[MemoryFact]], Awaitable[list[float]]]


def rank_by_mention(facts: Sequence[MemoryFact]) -> list[MemoryFact]:
    return sorted(facts, key=lambda f: (f.mention_count, f.decay_score), reverse=True)


def rank_by_graph_distance(
    facts: Sequence[MemoryFact], distances: Mapping[UUID, int]
) -> list[MemoryFact]:
    """Closest first; unknown distance sorts last rather than first.

    A missing edge in the fact graph is not evidence of closeness. Sorting unknowns
    first would let an unlinked fact outrank a fact the user mentioned alongside the
    query subject.
    """
    sentinel = max(distances.values(), default=0) + 1
    return sorted(
        facts,
        key=lambda f: (
            distances.get(f.id, sentinel),
            -f.decay_score,
        ),
    )


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[MemoryFact]], *, k: int = RRF_K
) -> list[MemoryFact]:
    """Merge several orderings without needing comparable scores.

    Facts absent from a ranking contribute nothing for it, which is what keeps a
    single strong stage from being cancelled by an unrelated weak one.
    """
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, fact in enumerate(ranking, start=1):
            scores[fact.id] = scores.get(fact.id, 0.0) + 1.0 / (k + rank)
    by_id = {f.id: f for f in rankings[0]} if rankings else {}
    for ranking in rankings[1:]:
        by_id.update({f.id: f for f in ranking})
    return sorted(by_id.values(), key=lambda f: scores.get(f.id, 0.0), reverse=True)


def mmr(
    facts: Sequence[MemoryFact], *, lambda_: float = 0.7, query_terms: Sequence[str] | None = None
) -> list[MemoryFact]:
    """Maximal marginal relevance over fact text.

    Similarity is token overlap, not embeddings: this runs before the model stage
    and must not cost a model call to remove three paraphrases of one preference.
    """
    terms = {t.lower() for t in (query_terms or [])}
    remaining = list(facts)
    selected: list[MemoryFact] = []
    base = {f.id: float(f.decay_score) * f.confidence for f in remaining}

    while remaining:
        best = None
        best_score = None
        for candidate in remaining:
            relevance = base[candidate.id]
            if terms:
                tokens = {t.lower() for t in candidate.fact.split()}
                overlap = len(tokens & terms) / max(1, len(terms))
                relevance = relevance + overlap
            penalty = max(
                (_overlap(candidate, chosen) for chosen in selected),
                default=0.0,
            )
            score = lambda_ * relevance - (1.0 - lambda_) * penalty
            if best_score is None or score > best_score:
                best, best_score = candidate, score
        if best is None:
            break
        selected.append(best)
        remaining = [f for f in remaining if f.id != best.id]
    return selected


def _overlap(left: MemoryFact, right: MemoryFact) -> float:
    a = {t.lower() for t in left.fact.split()}
    b = {t.lower() for t in right.fact.split()}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def rank_facts(
    query: str,
    facts: Sequence[MemoryFact],
    *,
    graph_distance: Mapping[UUID, int] | None = None,
    cross_encoder: CrossEncoderFn | None = None,
    top_n: int = 10,
    lambda_: float = 0.7,
) -> list[RankedFact]:
    """Run the ladder. Every stage is optional; each one that runs is recorded."""
    pool = list(facts)
    if not pool:
        return []

    stages: list[tuple[str, list[MemoryFact]]] = [("mention", rank_by_mention(pool))]
    if graph_distance:
        stages.append(("graph", rank_by_graph_distance(pool, graph_distance)))

    fused = reciprocal_rank_fusion([ranking for _, ranking in stages])
    diversified = mmr(fused, lambda_=lambda_, query_terms=query.split())
    survivors = diversified[:top_n]

    if cross_encoder is None or not survivors:
        return [
            RankedFact(fact=fact, score=float(fact.decay_score), stage="mmr")
            for fact in survivors
        ]

    scores = await cross_encoder(query, survivors)
    ordered = sorted(
        zip(survivors, scores, strict=False), key=lambda pair: pair[1], reverse=True
    )
    return [RankedFact(fact=fact, score=score, stage="cross_encoder") for fact, score in ordered]
