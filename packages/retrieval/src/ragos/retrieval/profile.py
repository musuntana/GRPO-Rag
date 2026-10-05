"""Stored profile row → runtime :class:`RetrieverProfile`.

Kept in the retrieval package rather than in the repository because the meaning
of a row's JSONB columns belongs to retrieval, not to storage: storage moves
rows, retrieval decides what a row is allowed to configure.

The mapping fails closed. A row whose budgets exceed 1.0, whose thresholds
overlap, or whose channel list has no budget is rejected rather than repaired —
silently substituting a default would mean the profile recorded on the eval
result is not the profile that ran.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.retrieval.types import (
    DEFAULT_CHANNEL_BUDGETS,
    CragThresholds,
    FusionConfig,
    RetrieverProfile,
)


class ProfileRow(Protocol):
    """The subset of ``app.retriever_profile_version`` this module reads."""

    id: UUID
    tenant_id: UUID
    profile_id: UUID
    version: int
    name: str | None
    query_strategy: str
    recall_channels: list[str]
    join_mode: str
    channel_budgets: dict[str, Any]
    fusion_config: dict[str, Any]
    crag_thresholds: dict[str, Any]
    crag_enabled: bool
    rerank_model_profile: str | None
    citation_verify_semantic: bool
    top_k_recall: int
    top_k_rerank: int
    top_k_generate: int


def profile_from_row(row: ProfileRow) -> RetrieverProfile:
    """Build the runtime profile, then assert its invariants.

    Empty JSONB columns fall back to the dataclass defaults rather than raising:
    a row written before a field existed and a row that deliberately left it
    empty mean the same thing, and the defaults are the documented values. A
    *malformed* value — the wrong type, an unknown strategy — is an error.
    """
    if int(row.version) < 1:
        raise ValidationError(
            "stored retriever profile has a non-positive version",
            version=int(row.version),
            row_id=str(row.id),
        )

    channels = list(row.recall_channels or []) or ["lexical"]
    budgets = dict(row.channel_budgets or {}) or {
        name: weight for name, weight in DEFAULT_CHANNEL_BUDGETS.items() if name in channels
    } or {name: 1.0 for name in channels}

    profile = RetrieverProfile(
        id=row.id,
        tenant_id=row.tenant_id,
        version=row.version,
        query_strategy=_require_choice(row.query_strategy, ("direct", "hyde", "decompose"),
                                       field="query_strategy", row_id=str(row.id)),
        recall_channels=channels,
        join_mode=_require_choice(row.join_mode,
                                  ("concatenate", "reciprocal_rank_fusion", "distribution_based"),
                                  field="join_mode", row_id=str(row.id)),
        channel_budgets=budgets,
        fusion_config=FusionConfig(
            join_mode=row.join_mode,
            **{
                key: value
                for key, value in (row.fusion_config or {}).items()
                if key in {"rrf_k", "layer_boost", "freshness_decay", "authority_boost"}
            },
        ),
        crag_thresholds=CragThresholds(
            **{
                key: float(value)
                for key, value in (row.crag_thresholds or {}).items()
                if key in {"relevant_at", "irrelevant_below"}
            }
        ),
        crag_enabled=bool(row.crag_enabled),
        rerank_model_profile=row.rerank_model_profile,
        citation_verify_semantic=bool(row.citation_verify_semantic),
        top_k_recall=int(row.top_k_recall),
        top_k_rerank=int(row.top_k_rerank),
        top_k_generate=int(row.top_k_generate),
    )

    try:
        profile.validate()
    except ValidationError as exc:
        raise ValidationError(
            f"stored retriever profile is invalid: {exc.message}",
            profile_id=str(row.profile_id),
            version=row.version,
            **exc.details,
        ) from exc
    return profile


def _require_choice(value: str | None, allowed: tuple[str, ...], *, field: str, row_id: str) -> str:
    if value in allowed:
        return str(value)
    raise ValidationError(
        "stored retriever profile has an unknown configuration value",
        field=field,
        value=value,
        supported=list(allowed),
        row_id=row_id,
    )


def latest_by_profile(rows: list[ProfileRow]) -> dict[UUID, ProfileRow]:
    """Reduce a version listing to the newest row per profile.

    Compared on ``version`` rather than insertion order: a version number that
    does not determine recency is a data problem, and picking by timestamp would
    hide it.
    """
    latest: dict[UUID, ProfileRow] = {}
    for row in rows:
        current = latest.get(row.profile_id)
        if current is None or row.version > current.version:
            latest[row.profile_id] = row
    return latest
