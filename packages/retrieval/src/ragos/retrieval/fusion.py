"""Pluggable fusion with per-channel token budgets.

Two rules from architecture §8.3 / §8.3.1 are enforced here rather than left to
call sites:

1. ``join_mode`` is selected by the retriever profile, not hardcoded.
2. Channels get a fixed budget each, and an empty channel does **not** donate
   its budget. Backfilling a silent channel re-introduces the dominance problem
   that per-channel budgets exist to prevent — an absent channel means the query
   genuinely had no lexical or memory signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ragos.core.errors import ValidationError
from ragos.retrieval.types import FusionConfig, ScoredChunk, same_evidence_lineage


@dataclass(slots=True)
class BudgetAllocation:
    channel: str
    budget: float
    weight: float
    returned: int


def validate_budgets(budgets: Mapping[str, float]) -> None:
    total = sum(budgets.values())
    if total > 1.0 + 1e-9:
        raise ValueError(f"channel budgets must sum to <= 1.0, got {total:.4f}")


def _apply_boosts(
    chunks: list[ScoredChunk],
    config: FusionConfig,
) -> list[ScoredChunk]:
    if not config.layer_boost and not config.freshness_decay and not config.authority_boost:
        return chunks
    boosted: list[ScoredChunk] = []
    for chunk in chunks:
        factor = config.layer_boost.get(chunk.layer, 1.0)
        boosted.append(
            ScoredChunk(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                kb_id=chunk.kb_id,
                text=chunk.text,
                score=chunk.score * factor,
                channel=chunk.channel,
                layer=chunk.layer,
                parent_chunk_id=chunk.parent_chunk_id,
                page_no=chunk.page_no,
                page_count=chunk.page_count,
                start_char_idx=chunk.start_char_idx,
                end_char_idx=chunk.end_char_idx,
                element_type=chunk.element_type,
                source_identity=chunk.source_identity,
                source_text=chunk.source_text,
                rerank_score=chunk.rerank_score,
            )
        )
    return boosted


def _rrf(channels: Mapping[str, list[ScoredChunk]], k: int) -> list[ScoredChunk]:
    """Reciprocal rank fusion.

    Rank-based rather than score-based because channel scores are not on a
    common scale — that is the normal case when mixing ``ts_rank_cd`` with cosine
    similarity, and it is why RRF is the default join mode.
    """
    scores: dict[object, float] = {}
    best: dict[object, ScoredChunk] = {}
    order: list[object] = []

    for channel_chunks in channels.values():
        for rank, chunk in enumerate(channel_chunks, start=1):
            if chunk.chunk_id not in scores:
                scores[chunk.chunk_id] = 0.0
                order.append(chunk.chunk_id)
                best[chunk.chunk_id] = chunk
            scores[chunk.chunk_id] += 1.0 / (k + rank)

    fused: list[ScoredChunk] = []
    for chunk_id in order:
        source = best[chunk_id]
        fused.append(
            ScoredChunk(
                chunk_id=source.chunk_id,
                doc_id=source.doc_id,
                kb_id=source.kb_id,
                text=source.text,
                score=scores[chunk_id],
                channel="fused",
                layer=source.layer,
                parent_chunk_id=source.parent_chunk_id,
                page_no=source.page_no,
                page_count=source.page_count,
                start_char_idx=source.start_char_idx,
                end_char_idx=source.end_char_idx,
                element_type=source.element_type,
                source_identity=source.source_identity,
                source_text=source.source_text,
            )
        )
    fused.sort(key=lambda c: c.score, reverse=True)
    return fused


def _weighted(channels: Mapping[str, list[ScoredChunk]]) -> list[ScoredChunk]:
    merged: dict[object, ScoredChunk] = {}
    scores: dict[object, float] = {}
    for channel_chunks in channels.values():
        for chunk in channel_chunks:
            merged.setdefault(chunk.chunk_id, chunk)
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + chunk.score
    fused = [
        ScoredChunk(
            chunk_id=cid,
            doc_id=merged[cid].doc_id,
            kb_id=merged[cid].kb_id,
            text=merged[cid].text,
            score=score,
            channel="fused",
            layer=merged[cid].layer,
            parent_chunk_id=merged[cid].parent_chunk_id,
            page_no=merged[cid].page_no,
            page_count=merged[cid].page_count,
            start_char_idx=merged[cid].start_char_idx,
            end_char_idx=merged[cid].end_char_idx,
            element_type=merged[cid].element_type,
            source_identity=merged[cid].source_identity,
            source_text=merged[cid].source_text,
        )
        for cid, score in scores.items()
    ]
    fused.sort(key=lambda c: c.score, reverse=True)
    return fused


def _distribution_based(
    channels: Mapping[str, list[ScoredChunk]],
    config: FusionConfig,
) -> list[ScoredChunk]:
    """Normalize each channel by its own score distribution, then sum.

    Only valid when per-channel score distributions are stable enough to
    normalize — hence opt-in rather than default.
    """
    normalized: dict[str, list[ScoredChunk]] = {}
    for name, channel_chunks in channels.items():
        if not channel_chunks:
            continue
        scores = [c.score for c in channel_chunks]
        mean = sum(scores) / len(scores)
        var = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = var**0.5
        if std == 0.0:
            normalized[name] = channel_chunks
            continue
        normalized[name] = [
            ScoredChunk(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                kb_id=c.kb_id,
                text=c.text,
                score=(c.score - mean) / std,
                channel=c.channel,
                layer=c.layer,
                parent_chunk_id=c.parent_chunk_id,
                page_no=c.page_no,
                page_count=c.page_count,
                start_char_idx=c.start_char_idx,
                end_char_idx=c.end_char_idx,
                element_type=c.element_type,
                source_identity=c.source_identity,
                source_text=c.source_text,
            )
            for c in channel_chunks
        ]

    config_boosted = {name: _apply_boosts(chunks, config) for name, chunks in normalized.items()}
    return _weighted(config_boosted)


def _concatenate(channels: Mapping[str, list[ScoredChunk]]) -> list[ScoredChunk]:
    seen: set[object] = set()
    out: list[ScoredChunk] = []
    for channel_chunks in channels.values():
        for chunk in channel_chunks:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            out.append(chunk)
    out.sort(key=lambda c: c.score, reverse=True)
    return out


class Fusioner:
    def fuse(
        self,
        channels: Mapping[str, list[ScoredChunk]],
        config: FusionConfig,
    ) -> list[ScoredChunk]:
        identities: dict[object, ScoredChunk] = {}
        for chunks in channels.values():
            for chunk in chunks:
                previous = identities.get(chunk.chunk_id)
                if previous is not None and not same_evidence_lineage(previous, chunk):
                    raise ValidationError("same chunk id has conflicting evidence lineage")
                identities[chunk.chunk_id] = chunk
        active = {name: chunks for name, chunks in channels.items() if chunks}
        if not active:
            return []

        if config.join_mode == "reciprocal_rank_fusion":
            fused = _rrf(active, config.rrf_k)
        elif config.join_mode == "weighted":
            fused = _weighted(active)
        elif config.join_mode == "distribution_based":
            fused = _distribution_based(active, config)
        else:
            fused = _concatenate(active)

        return _apply_boosts(fused, config)


def allocate_by_budget(
    channels: Mapping[str, list[ScoredChunk]],
    budgets: Mapping[str, float],
    *,
    total_token_budget: int = 2000,
    tokens_per_chunk: int = 200,
) -> tuple[dict[str, list[ScoredChunk]], list[BudgetAllocation]]:
    """Trim each channel to its own share of the total token budget.

    Each channel gets ``budget × total_token_budget`` tokens. A channel is
    trimmed independently and an **empty channel keeps its budget unspent** —
    an absent lexical channel means the query had no lexical signal, and handing
    that budget to dense recall is how one channel silently becomes the whole
    context.

    A channel with a non-zero budget always keeps at least one chunk: a budget
    that rounds down to zero items would make recall for that channel
    structurally impossible, which is a worse failure than a slightly oversized
    context.
    """
    validate_budgets(budgets)
    if tokens_per_chunk <= 0:
        raise ValueError("tokens_per_chunk must be positive")

    trimmed: dict[str, list[ScoredChunk]] = {}
    allocations: list[BudgetAllocation] = []

    for name, chunks in channels.items():
        weight = budgets.get(name, 0.0)
        if weight <= 0.0:
            allocations.append(BudgetAllocation(name, 0.0, weight, 0))
            continue
        allowance = weight * total_token_budget
        allowed = int(allowance // tokens_per_chunk)
        if chunks and allowed < 1:
            allowed = 1
        kept = list(chunks)[:allowed]
        trimmed[name] = kept
        allocations.append(
            BudgetAllocation(
                channel=name,
                budget=weight,
                weight=weight,
                returned=len(kept),
            )
        )

    return trimmed, allocations
