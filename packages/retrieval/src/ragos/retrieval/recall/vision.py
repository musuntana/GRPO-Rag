"""Page-image (vision) recall — per-KB opt-in, Phase 2.

Design constraints that are not negotiable:

* **A separate collection.** A vision embedding and a text embedding in one
  collection make every distance meaningless (architecture §6.3.4). The vision
  collection is named by its own profile.
* **A seventh channel with its own budget.** It joins the per-channel budget
  allocation rather than being appended to an existing channel, because a
  channel without a budget is exactly the dominance problem §8.3.1 exists to
  prevent.
* **The predicate is still mandatory.** ``tenant_id`` is a required predicate
  like every other read, built by the shared filter builder.

Table and figure *structure* does not depend on any of this — it lands at parse
time as typed text (ADR-0006). This module adds pixels, not structure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import RetrieverProfile, ScoredChunk

LOGGER = get_logger(__name__)

#: Default share of the context budget. Deliberately small: a page image is a
#: coarse matching target, and this channel complements text recall rather than
#: competing with it.
DEFAULT_VISION_BUDGET = 0.10

#: Budget weights must still sum to <= 1.0 with the vision channel included.
DEFAULT_BUDGETS_WITH_VISION: dict[str, float] = {
    "lexical": 0.20,
    "dense": 0.40,
    "memory": 0.15,
    "hot": 0.15,
    "vision": 0.10,
}


@dataclass(slots=True)
class VisionProfile:
    """A vision embedding configuration, resolved through the gateway."""

    profile_id: str
    collection: str
    embed: Callable[[str], list[float]]
    enabled: bool = False


def budgets_with_vision(
    base: dict[str, float],
    *,
    vision_weight: float = DEFAULT_VISION_BUDGET,
) -> dict[str, float]:
    """Reallocate budgets to admit the vision channel.

    The dense share absorbs the difference rather than the sum being allowed to
    exceed 1.0 — silently overshooting means the channels collectively promise
    more context than the budget allows, and the excess goes to whichever
    channel happened to return the largest raw scores.
    """
    if vision_weight < 0.0:
        raise ValidationError("vision_weight must be non-negative", weight=vision_weight)
    remaining = 1.0 - vision_weight
    others = {k: v for k, v in base.items() if k != "vision"}
    total = sum(others.values())
    if total <= 0.0:
        raise ValidationError("base budgets must contain at least one non-vision channel")
    scaled = {k: (v / total) * remaining for k, v in others.items()}
    scaled["vision"] = vision_weight
    return scaled


class VisionRecall:
    """Recall page images from the vision collection."""

    def __init__(self, client, profile: VisionProfile) -> None:
        self.client = client
        self.profile = profile

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        retriever_profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        if not self.profile.enabled:
            LOGGER.debug("vision recall disabled for this profile; skipping")
            return []

        tenant_ctx.require_scope_key()
        raise ValidationError(
            "vision recall unavailable: authoritative representation hydration is not connected"
        )


@dataclass(slots=True)
class VisionPagePoint:
    """One page image to embed and index."""

    page_id: UUID
    doc_id: UUID
    kb_id: UUID | None
    page_no: int
    image_ref: str
    caption: str | None = None


def vision_payload(point: VisionPagePoint, tenant_id: UUID) -> dict:
    """Payload for a page-image point.

    ``tenant_id`` is required and is not derived from the point — the point
    carries no tenant, and a payload assembled without one would be readable by
    every tenant that queried the collection.
    """
    return {
        "tenant_id": str(tenant_id),
        "doc_id": str(point.doc_id),
        "kb_id": str(point.kb_id) if point.kb_id else None,
        "page_id": str(point.page_id),
        "page_no": point.page_no,
        "layer": "L1",
        "element_type": "figure",
        "image_ref": point.image_ref,
        "caption": point.caption,
        "modality": "image",
    }


def _as_uuid(value) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None
