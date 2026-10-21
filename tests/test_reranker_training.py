"""Reranker training: mining keeps grades, GRPO is gated, promotion is gated.

ADR-0007. The two claims under test are that (a) collapsing grade 1 into grade 0
destroys the miner's output, and (b) GRPO must beat the SFT baseline to be
adopted — the baseline ships otherwise.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.retrieval.metrics import Grade
from ragos.retrieval.training.grpo import (
    GroupBatch,
    RankedPage,
    Ranking,
    RewardWeights,
    compute_reward,
    evaluate_promotion,
    group_relative_advantages,
    hard_negative_demotion,
    kl_penalty,
)
from ragos.retrieval.training.mine import (
    MiningConfig,
    MiningPair,
    mine,
    mine_case,
    split_by_grade,
)
from ragos.retrieval.training.promote import (
    PromotionRequest,
    artifact_path,
    promote,
    register_as_model_profile,
    stale_artifact_reason,
)
from ragos.retrieval.training.sft import SFTConfig, build_examples, dataset_digest, train
from ragos.retrieval.types import EvalCase, GoldEvidence


def _case() -> EvalCase:
    """The canonical hard-negative shape: Q2 revenue, with Q1 and Q3 beside it."""
    return EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="2025 Q2 revenue?",
        gold_evidence=[
            GoldEvidence(doc_id="doc-18", grade=3, page_no=18),
            GoldEvidence(doc_id="doc-17", grade=1, page_no=17),
            GoldEvidence(doc_id="doc-19", grade=1, page_no=19),
            GoldEvidence(doc_id="doc-42", grade=1, page_no=42),
            GoldEvidence(doc_id="doc-hr", grade=0),
        ],
        split="dev",
    )


# -- mining -----------------------------------------------------------------


def test_mining_prefers_the_retriever_hard_negatives() -> None:
    result = mine_case(_case())

    negatives = {p.negative_doc_id for p in result.pairs}
    assert "doc-hr" in negatives, "an off-topic page is easy and still mined"
    assert negatives & {"doc-17", "doc-19", "doc-42"}, "adjacent pages must be mined"


def test_mined_negatives_keep_their_grade() -> None:
    result = mine_case(_case())

    related, irrelevant = split_by_grade(result.pairs)
    assert all(p.negative_grade == 1 for p in related)
    assert all(p.negative_grade == 0 for p in irrelevant)
    # The mix is dominated by grade 1, which is the point: those are the
    # negatives a random sampler would never produce.
    assert result.related_count > result.irrelevant_count


def test_mining_caps_grade_one_negatives_per_positive() -> None:
    """Uncapped mining fills the batch with related pages and over-rejects."""
    result = mine_case(_case(), config=MiningConfig(max_related_per_positive=1))

    related, _ = split_by_grade(result.pairs)
    assert len(related) == 1


def test_mining_labels_adjacent_and_same_document_negatives_differently() -> None:
    result = mine_case(_case())
    reasons = {p.negative_doc_id: p.negative_reason for p in result.pairs}

    assert reasons["doc-17"] == "adjacent_page"
    assert reasons["doc-42"] == "cross_topic"


def test_mining_skips_cases_with_no_answer_bearing_page() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="q",
        gold_evidence=[GoldEvidence(doc_id="doc-1", grade=1)],
    )

    result = mine_case(case)

    assert result.pairs == []
    assert result.cases_without_positive == 1


def test_mining_rejects_negative_caps() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        mine([_case()], config=MiningConfig(max_related_per_positive=-1))


def test_same_pdf_different_page_is_mined_as_hard_negative() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="How long is the warranty?",
        gold_evidence=[
            GoldEvidence(doc_id="contract", grade=3, page_no=7),
            GoldEvidence(doc_id="contract", grade=1, page_no=8),
        ],
    )

    mined = mine_case(case)

    assert len(mined.pairs) == 1
    pair = mined.pairs[0]
    assert (pair.positive_doc_id, pair.positive_page_no) == ("contract", 7)
    assert (pair.negative_doc_id, pair.negative_page_no) == ("contract", 8)
    assert pair.negative_reason == "same_document"


# -- SFT baseline -----------------------------------------------------------


def test_sft_weights_grade_one_negatives_above_grade_zero() -> None:
    mined = mine([_case()])
    examples = build_examples(
        mined.pairs,
        {
            ("doc-18", 18): "Q2 table",
            ("doc-17", 17): "Q1 table",
            ("doc-19", 19): "Q3 table",
            ("doc-42", 42): "FY table",
            ("doc-hr", None): "leave policy",
        },
    )

    related = [e for e in examples if e.negative_grade == 1]
    irrelevant = [e for e in examples if e.negative_grade == 0]
    assert related and irrelevant
    assert related[0].weight > irrelevant[0].weight


def test_sft_dataset_digest_is_stable_and_content_addressed() -> None:
    mined = mine([_case()])
    texts = {
        ("doc-18", 18): "Q2",
        ("doc-17", 17): "Q1",
        ("doc-19", 19): "Q3",
        ("doc-42", 42): "FY",
        ("doc-hr", None): "HR",
    }

    first = train(mined.pairs, texts, tenant_scope_key="t::u")
    second = train(mined.pairs, texts, tenant_scope_key="t::u")

    assert first.dataset_digest == second.dataset_digest
    assert first.related_count > 0


def test_sft_run_records_the_tenant_scope_key() -> None:
    mined = mine([_case()])
    run = train(
        mined.pairs,
        {
            ("doc-18", 18): "a",
            ("doc-17", 17): "b",
            ("doc-19", 19): "c",
            ("doc-42", 42): "d",
            ("doc-hr", None): "e",
        },
        tenant_scope_key="tenant-1::user-9",
    )

    assert run.tenant_scope_key == "tenant-1::user-9"
    assert run.method == "sft"


def test_sft_rejects_a_zero_related_weight() -> None:
    with pytest.raises(ValueError, match="related_weight"):
        SFTConfig(related_weight=0.0).validate()


def test_dataset_digest_changes_when_the_pairs_change() -> None:
    a = MiningPair(
        case_id=uuid4(),
        question="q",
        positive_doc_id="p",
        positive_page_no=1,
        positive_grade=3,
        negative_doc_id="n",
        negative_page_no=2,
        negative_grade=1,
        negative_reason="adjacent_page",
    )
    b = MiningPair(
        case_id=a.case_id,
        question="q",
        positive_doc_id="p",
        positive_page_no=1,
        positive_grade=3,
        negative_doc_id="other",
        negative_page_no=3,
        negative_grade=1,
        negative_reason="adjacent_page",
    )
    texts = {
        ("p", 1): "positive",
        ("n", 2): "neg a",
        ("other", 3): "neg b",
    }

    assert dataset_digest(build_examples([a], texts)) != dataset_digest(
        build_examples([b], texts)
    )


def _same_pdf_case() -> EvalCase:
    return EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="What is the warranty period?",
        gold_evidence=[
            GoldEvidence(doc_id="contract", page_no=7, grade=3),
            GoldEvidence(doc_id="contract", page_no=8, grade=1),
        ],
    )


def test_sft_uses_distinct_texts_for_same_pdf_pages() -> None:
    case = _same_pdf_case()
    pairs = mine_case(case).pairs
    examples = build_examples(
        pairs, {("contract", 7): "24 months", ("contract", 8): "5% deposit"}
    )
    assert len(examples) == 1
    example = examples[0]
    assert (example.positive_text, example.negative_text) == (
        "24 months", "5% deposit"
    )
    assert (example.positive_page_no, example.negative_page_no) == (7, 8)


def test_sft_fails_closed_when_only_document_wide_text_is_supplied() -> None:
    case = _same_pdf_case()
    pairs = mine_case(case).pairs
    with pytest.raises(ValidationError, match="document/page"):
        build_examples(pairs, {"contract": "whole PDF"})


def test_sft_fails_closed_on_one_missing_page_instead_of_dropping_pair() -> None:
    case = _same_pdf_case()
    pairs = mine_case(case).pairs
    with pytest.raises(ValidationError, match="document/page"):
        build_examples(pairs, {("contract", 7): "24 months"})


def test_sft_unpaged_legacy_lookup_remains_compatible() -> None:
    case_id = uuid4()
    pair = MiningPair(
        case_id=case_id,
        question="q",
        positive_doc_id="a",
        positive_page_no=None,
        positive_grade=3,
        negative_doc_id="b",
        negative_page_no=None,
        negative_grade=1,
        negative_reason="cross_topic",
    )
    examples = build_examples([pair], {"a": "correct", "b": "wrong"})
    assert len(examples) == 1
    assert examples[0].positive_text == "correct"


def test_sft_digest_changes_with_page_identity_even_for_identical_text() -> None:
    case_id = uuid4()
    def make_pair(page: int) -> MiningPair:
        return MiningPair(
            case_id=case_id,
            question="q",
            positive_doc_id="contract",
            positive_page_no=page,
            positive_grade=3,
            negative_doc_id="contract",
            negative_page_no=8,
            negative_grade=1,
            negative_reason="same_document",
        )
    texts = {
        ("contract", 6): "same text",
        ("contract", 7): "same text",
        ("contract", 8): "different text",
    }
    first = dataset_digest(build_examples([make_pair(6)], texts))
    second = dataset_digest(build_examples([make_pair(7)], texts))
    assert first != second


def test_sft_digest_changes_when_weight_changes() -> None:
    case = _same_pdf_case()
    pairs = mine_case(case).pairs
    texts = {("contract", 7): "24 months", ("contract", 8): "deposit"}
    a = dataset_digest(build_examples(pairs, texts, SFTConfig(related_weight=1.0)))
    b = dataset_digest(build_examples(pairs, texts, SFTConfig(related_weight=2.0)))
    assert a != b


# -- GRPO reward ------------------------------------------------------------


def test_reward_rewards_promoting_the_answer_above_related_pages() -> None:
    case = _case()
    mined = mine([case])

    good = compute_reward(
        Ranking(
            str(case.id),
            [RankedPage("doc-18", 18), RankedPage("doc-17", 17), RankedPage("doc-19", 19)],
        ),
        case,
        mined.pairs,
    )
    bad = compute_reward(
        Ranking(
            str(case.id),
            [RankedPage("doc-17", 17), RankedPage("doc-18", 18), RankedPage("doc-19", 19)],
        ),
        case,
        mined.pairs,
    )

    assert good.total > bad.total
    assert good.hit == 1.0


def test_hard_negative_demotion_measures_whether_related_pages_sink() -> None:
    assert hard_negative_demotion(["pos", "neg1", "neg2"], ["neg1", "neg2"]) > 0.0
    assert hard_negative_demotion(["neg1", "neg2", "pos"], ["neg1", "neg2"]) == 0.0
    # No negatives at all cannot be a failure.
    assert hard_negative_demotion(["pos"], []) == 1.0


def test_group_relative_advantages_are_centred() -> None:
    advantages = group_relative_advantages([1.0, 0.6, 0.2, 0.0])

    assert sum(advantages) == pytest.approx(0.0, abs=1e-9)
    assert advantages[0] > 0 > advantages[-1]


def test_group_relative_advantages_survive_an_identical_group() -> None:
    """A group where every sample is equal carries no ranking signal."""
    advantages = group_relative_advantages([0.5, 0.5, 0.5])

    assert all(a == pytest.approx(0.0) for a in advantages)


def test_kl_penalty_is_zero_for_an_identical_policy() -> None:
    assert kl_penalty([1.0, 2.0], [1.0, 2.0]) == 0.0


def test_kl_penalty_is_positive_when_the_policy_drifts() -> None:
    assert kl_penalty([2.0, 2.0], [1.0, 1.0]) > 0.0


def test_reward_weights_must_not_all_be_zero() -> None:
    with pytest.raises(ValueError, match="all be zero"):
        RewardWeights(alpha=0.0, beta=0.0, gamma=0.0).validate()


def test_group_batch_exposes_rewards_and_advantages_together() -> None:
    case = _case()
    mined = mine([case])
    batch = GroupBatch(
        case=case,
        rankings=[
            Ranking(str(case.id), [RankedPage("doc-18", 18), RankedPage("doc-17", 17)]),
            Ranking(str(case.id), [RankedPage("doc-17", 17), RankedPage("doc-18", 18)]),
        ],
        pairs=mined.pairs,
    )

    rewards = batch.rewards()
    advantages = batch.advantages()

    assert len(rewards) == len(advantages) == 2
    assert advantages[0] > advantages[1]


def test_grpo_reward_distinguishes_pages_with_the_same_document_id() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="How long is the warranty?",
        gold_evidence=[
            GoldEvidence(doc_id="contract", grade=3, page_no=7),
            GoldEvidence(doc_id="contract", grade=1, page_no=8),
        ],
    )
    pairs = mine_case(case).pairs
    correct = compute_reward(
        Ranking(str(case.id), [RankedPage("contract", 7), RankedPage("contract", 8)]),
        case,
        pairs,
    )
    wrong = compute_reward(
        Ranking(str(case.id), [RankedPage("contract", 8), RankedPage("contract", 7)]),
        case,
        pairs,
    )

    assert correct.hit == wrong.hit == 1.0
    assert correct.ndcg > wrong.ndcg
    assert correct.hard_negative_demotion == 1.0
    assert wrong.hard_negative_demotion == 0.0
    assert correct.total > wrong.total


def test_grpo_does_not_copy_gold_page_when_sample_has_only_doc_id() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="What is the warranty?",
        gold_evidence=[GoldEvidence(doc_id="contract", grade=3, page_no=7)],
    )
    reward = compute_reward(Ranking(str(case.id), ["contract"]), case, [])

    assert reward.hit == 0.0
    assert reward.ndcg == 0.0


def test_grpo_wrong_page_only_cannot_claim_hit() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="What is the warranty?",
        gold_evidence=[
            GoldEvidence(doc_id="contract", grade=3, page_no=7),
            GoldEvidence(doc_id="contract", grade=1, page_no=8),
        ],
    )
    reward = compute_reward(
        Ranking(str(case.id), [RankedPage("contract", 8)]), case, mine_case(case).pairs
    )

    assert reward.hit == 0.0
    assert reward.ndcg < 1.0
    assert reward.hard_negative_demotion == 0.0


def test_hard_negative_reward_does_not_increase_with_unrelated_padding() -> None:
    assert hard_negative_demotion(
        ["neg", "pos"], ["neg"], positives=["pos"]
    ) == hard_negative_demotion(
        ["noise", "neg", "pos"], ["neg"], positives=["pos"]
    ) == 0.0


def test_missing_negative_is_not_rewarded_as_demoted() -> None:
    assert hard_negative_demotion(["pos"], ["neg"], positives=["pos"]) == 0.0


def test_duplicate_pages_in_sample_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate evidence"):
        Ranking("case", [RankedPage("contract", 7), RankedPage("contract", 7)])


def test_grpo_rejects_reward_with_foreign_case_id() -> None:
    case = _case()
    with pytest.raises(ValidationError, match="case_id"):
        compute_reward(Ranking("other-case", [RankedPage("doc-18", 18)]), case, [])


# -- the gate ---------------------------------------------------------------


def test_grpo_that_loses_to_the_baseline_does_not_ship() -> None:
    decision = evaluate_promotion(
        baseline_metrics={"evidence_hit_rate": 0.86, "citation_accuracy": 0.94},
        candidate_metrics={"evidence_hit_rate": 0.80, "citation_accuracy": 0.95},
    )

    assert not decision.promotable
    assert any("baseline" in r for r in decision.reasons)


def test_grpo_that_ties_the_baseline_does_not_ship() -> None:
    """Ties go to the simpler, already-shipped artifact."""
    decision = evaluate_promotion(
        baseline_metrics={"evidence_hit_rate": 0.86},
        candidate_metrics={"evidence_hit_rate": 0.86},
    )

    assert not decision.promotable


def test_grpo_that_wins_on_every_metric_ships() -> None:
    decision = evaluate_promotion(
        baseline_metrics={"evidence_hit_rate": 0.80, "citation_accuracy": 0.90},
        candidate_metrics={"evidence_hit_rate": 0.86, "citation_accuracy": 0.94},
    )

    assert decision.promotable


# -- promotion --------------------------------------------------------------


TENANT_ID = uuid4()
SCOPE_KEY = f"{TENANT_ID}:ws1:u1"


def _request(**overrides) -> PromotionRequest:
    mined = mine([_case()])
    run = train(
        mined.pairs,
        {
            ("doc-18", 18): "a",
            ("doc-17", 17): "b",
            ("doc-19", 19): "c",
            ("doc-42", 42): "d",
            ("doc-hr", None): "e",
        },
        tenant_scope_key=SCOPE_KEY,
    )
    defaults = {
        "tenant_id": TENANT_ID,
        "scope_key": SCOPE_KEY,
        "run": run,
        "eval_bundle_id": "b1",
        "eval_metrics": {"evidence_hit_rate": 0.86},
        "eval_split": "holdout",
        "dataset_split": "dev",
    }
    defaults.update(overrides)
    return PromotionRequest(**defaults)


def test_artifact_path_is_scoped_to_the_tenant() -> None:
    assert artifact_path(SCOPE_KEY, 3) == f"{SCOPE_KEY}/models/reranker/3/"


def test_artifact_path_refuses_an_empty_scope_key() -> None:
    with pytest.raises(PermissionDeniedError, match="unscoped"):
        artifact_path("", 1)


def test_promotion_fails_closed_without_a_scope_key() -> None:
    outcome = promote(_request(scope_key=""))

    assert not outcome.promoted
    assert any("failing closed" in r for r in outcome.reasons)


def test_promotion_refuses_a_scope_key_from_another_tenant() -> None:
    outcome = promote(_request(scope_key="other:ws:u"))

    assert not outcome.promoted
    assert any("does not belong" in r for r in outcome.reasons)


def test_a_train_set_improvement_is_treated_as_a_failed_run() -> None:
    outcome = promote(_request(eval_split="dev", dataset_split="dev"))

    assert not outcome.promoted
    assert any("tuned on" in r for r in outcome.reasons)


def test_promotion_records_a_baseline_regression() -> None:
    outcome = promote(
        _request(eval_metrics={"evidence_hit_rate": 0.70}),
        baseline_metrics={"evidence_hit_rate": 0.86},
    )

    assert not outcome.promoted


def test_clean_promotion_yields_a_scoped_artifact_ref() -> None:
    outcome = promote(_request())

    assert outcome.promoted
    assert outcome.artifact_ref.startswith(f"{SCOPE_KEY}/models/reranker/")


def test_registered_profile_is_an_ordinary_rerank_profile() -> None:
    """No special-cased model type: the gateway path stays uniform."""
    outcome = promote(_request())

    profile = register_as_model_profile(outcome, profile_name="rerank-v3", tenant_id=uuid4())

    assert profile["use_case"] == "rerank"
    assert profile["artifact_ref"] == outcome.artifact_ref


def test_unpromoted_artifact_cannot_be_registered() -> None:
    from ragos.retrieval.training.promote import PromotionOutcome

    with pytest.raises(PermissionDeniedError, match="not promoted"):
        register_as_model_profile(
            PromotionOutcome(promoted=False, reasons=["regressed"]),
            profile_name="x",
            tenant_id=uuid4(),
        )


# -- staleness --------------------------------------------------------------


def test_artifact_is_stale_when_its_dataset_changed() -> None:
    reason = stale_artifact_reason(
        artifact_dataset_digest="old",
        current_dataset_digest="new",
        artifact_promoted_at="2026-10-01T00:00:00+00:00",
    )

    assert reason == "training dataset changed since promotion"


def test_fresh_artifact_is_not_stale() -> None:
    assert (
        stale_artifact_reason(
            artifact_dataset_digest="same",
            current_dataset_digest="same",
            artifact_promoted_at="2026-10-03T00:00:00+00:00",
        )
        is None
    )


def test_old_artifact_is_stale_without_re_evaluation() -> None:
    assert (
        stale_artifact_reason(
            artifact_dataset_digest="same",
            current_dataset_digest="same",
            artifact_promoted_at="2020-01-01T00:00:00+00:00",
            max_age_days=90,
        )
        is not None
    )


def test_grade_helper_is_keyed_by_document() -> None:
    """Graded retrieval input uses documents, so a page parent is not double-counted."""
    assert Grade("doc-18", 3).doc_id == "doc-18"