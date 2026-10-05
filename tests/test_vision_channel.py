"""Page-image (vision) recall: opt-in, separately budgeted, separately collected.

Architecture §6.3.4 and §8.3.1. The claims under test are that admitting a
seventh channel cannot push the budget sum above 1.0, and that a vision
embedding never lands in a text collection.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.recall.vision import (
    DEFAULT_BUDGETS_WITH_VISION,
    DEFAULT_VISION_BUDGET,
    VisionPagePoint,
    VisionProfile,
    budgets_with_vision,
    vision_payload,
)
from ragos.retrieval.types import RetrieverProfile
from ragos.runtime.vector import QdrantVectorIndexWriter, build_filter_spec
from ragos.storage.repositories.search import SearchTier


def test_admitting_the_vision_channel_keeps_the_sum_at_one() -> None:
    budgets = budgets_with_vision({"lexical": 0.20, "dense": 0.45, "memory": 0.15, "hot": 0.20})

    assert sum(budgets.values()) == pytest.approx(1.0)


def test_the_vision_share_comes_out_of_the_other_channels() -> None:
    budgets = budgets_with_vision(
        {"lexical": 0.20, "dense": 0.45, "memory": 0.15, "hot": 0.20},
        vision_weight=0.10,
    )

    assert budgets["vision"] == pytest.approx(DEFAULT_VISION_BUDGET)
    # Dense absorbs the difference; the sum is never allowed to overshoot,
    # because the excess goes to whichever channel returned the largest scores.
    assert budgets["dense"] < 0.45


def test_the_default_budget_set_with_vision_sums_to_one() -> None:
    assert sum(DEFAULT_BUDGETS_WITH_VISION.values()) == pytest.approx(1.0)


def test_a_negative_vision_weight_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        budgets_with_vision({"dense": 1.0}, vision_weight=-0.1)


def test_a_base_with_no_other_channel_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one non-vision"):
        budgets_with_vision({"vision": 1.0}, vision_weight=0.1)


def test_a_profile_carrying_the_vision_channel_validates() -> None:
    profile = RetrieverProfile(
        id=uuid4(),
        tenant_id=uuid4(),
        recall_channels=["dense", "vision"],
        channel_budgets=dict(DEFAULT_BUDGETS_WITH_VISION),
    )

    profile.validate()


def test_a_profile_over_budget_with_vision_is_rejected() -> None:
    profile = RetrieverProfile(
        id=uuid4(),
        tenant_id=uuid4(),
        recall_channels=["dense", "vision"],
        channel_budgets=dict(DEFAULT_BUDGETS_WITH_VISION, dense=0.9),
    )

    with pytest.raises(ValidationError, match="sum to <= 1.0"):
        profile.validate()


def test_vision_recall_declines_when_the_profile_has_no_vision_budget() -> None:
    """A channel without a budget is the dominance problem §8.3.1 prevents."""
    import asyncio

    from ragos.retrieval.recall.vision import VisionRecall

    ctx = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="u", trace_id="t"
    )
    recall = VisionRecall(client=object(), profile=VisionProfile(
        profile_id="vp", collection="c", embed=lambda _: [0.0], enabled=True
    ))
    profile = RetrieverProfile(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        recall_channels=["vision"],
        channel_budgets={"dense": 1.0},
    )

    assert asyncio.run(recall.recall("q", ctx, profile)) == []


def test_vision_recall_returns_nothing_when_disabled() -> None:
    import asyncio

    from ragos.retrieval.recall.vision import VisionRecall

    ctx = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="u", trace_id="t"
    )
    recall = VisionRecall(client=object(), profile=VisionProfile(
        profile_id="vp", collection="c", embed=lambda _: [0.0], enabled=False
    ))
    profile = RetrieverProfile(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        recall_channels=["vision"],
        channel_budgets=dict(DEFAULT_BUDGETS_WITH_VISION),
    )

    assert asyncio.run(recall.recall("q", ctx, profile)) == []


def test_the_vision_payload_carries_the_tenant() -> None:
    """The point carries no tenant; a payload without one is readable by everyone."""
    tenant = uuid4()
    point = VisionPagePoint(
        page_id=uuid4(),
        doc_id=uuid4(),
        kb_id=uuid4(),
        page_no=18,
        image_ref="t/ws/u/models/docs/d/v1/page_18.png",
    )

    payload = vision_payload(point, tenant)

    assert payload["tenant_id"] == str(tenant)
    assert payload["modality"] == "image"
    assert payload["element_type"] == "figure"


def test_the_vision_collection_is_distinct_from_the_text_collection() -> None:
    writer = QdrantVectorIndexWriter(client=object(), embedding_dim=64)

    text_collection = writer.collection_name(SearchTier.STABLE)
    vision_collection = writer.vision_collection_name("siglip_base")

    assert text_collection != vision_collection
    assert "bge_m3" in text_collection or writer.embedding_profile in text_collection


def test_a_vision_filter_still_requires_the_tenant_predicate() -> None:
    spec = build_filter_spec(f"{uuid4()}:ws:u")

    assert spec.must[0].key == "tenant_id"


def test_an_empty_scope_key_is_refused_for_the_vision_path_too() -> None:
    with pytest.raises(PermissionDeniedError):
        build_filter_spec("")