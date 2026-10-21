"""Page-aware ranking evaluator regressions and frozen JSONL trace contract."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.retrieval.eval_svc import CaseOutcome, default_bundle, run_eval
from ragos.retrieval.metrics import (
    Grade,
    evaluate_retrieval,
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    unique_evidence,
)
from ragos.retrieval.offline_eval import evaluate_trace_bytes, main
from ragos.retrieval.types import EvalCase, GoldEvidence


def _trace(*, retrieved=None, gold=None, case_id=None, split="holdout"):
    tenant = "00000000-0000-4000-8000-000000000100"
    index = "00000000-0000-4000-8000-000000000101"
    header = {
        "type": "manifest",
        "schema_version": 1,
        "dataset_id": "synthetic-contracts-v1",
        "tenant_id": tenant,
        "split": split,
        "index_id": index,
        "embedding_profile": "qwen3_embedding",
        "embedding_version": "frozen-model-digest-v1",
        "retriever_version": "dense-baseline-v1",
    }
    record = {
        "type": "case",
        "case_id": case_id or "00000000-0000-4000-8000-000000000200",
        "split": split,
        "question": "What is the contract warranty?",
        "gold": gold if gold is not None else [
            {"doc_id": "contract", "page_no": 7, "grade": 3}
        ],
        "retrieved": retrieved if retrieved is not None else [
            {"doc_id": "contract", "page_no": 8},
            {"doc_id": "contract", "page_no": 7},
        ],
    }
    return [header, record]


def _bytes(lines):
    return ("\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n").encode()


def test_repeated_chunks_for_same_page_do_not_inflate_ranking() -> None:
    gold = [GoldEvidence(doc_id="contract", page_no=7, grade=3)]
    ranked = [
        Grade(doc_id="contract", page_no=8, grade=0),
        Grade(doc_id="contract", page_no=8, grade=0),
        Grade(doc_id="contract", page_no=7, grade=0),
        Grade(doc_id="contract", page_no=7, grade=0),
    ]

    assert [(p.doc_id, p.page_no) for p in unique_evidence(ranked)] == [
        ("contract", 8), ("contract", 7)
    ]
    assert precision_at_k(ranked, gold, 10) == 0.5
    assert recall_at_k(ranked, gold, 50) == 1.0
    assert hit_at_k(ranked, gold, 1) == 0.0
    assert hit_at_k(ranked, gold, 2) == 1.0
    assert reciprocal_rank(ranked, gold) == 0.5
    assert 0.0 < ndcg_at_k(ranked, gold, 10) < 1.0


def test_ndcg_never_exceeds_one_due_to_repeated_gold_page() -> None:
    gold = [GoldEvidence(doc_id="contract", page_no=7, grade=3)]
    repeated = [Grade("contract", 3, 7) for _ in range(10)]
    assert ndcg_at_k(repeated, gold, 10) == 1.0


def test_aggregated_mrr_at_10_cuts_off_after_ten_unique_pages() -> None:
    case = EvalCase(
        id=uuid4(),
        tenant_id=uuid4(),
        question="warranty?",
        gold_evidence=[GoldEvidence(doc_id="contract", page_no=7, grade=3)],
    )
    outside = [
        Grade(doc_id="contract", page_no=i, grade=0) for i in range(1, 12)
        if i != 7
    ]
    assert len(outside) == 10
    outside.append(Grade(doc_id="contract", page_no=7, grade=0))
    assert evaluate_retrieval([case], [outside]).mrr_at_10 == 0.0
    # Ten chunk copies of the same wrong page consume only one page ranking slot.
    duplicated = [Grade(doc_id="contract", page_no=8, grade=0)] * 10
    duplicated.append(Grade(doc_id="contract", page_no=7, grade=0))
    assert evaluate_retrieval([case], [duplicated]).mrr_at_10 == 0.5


def test_frozen_trace_outputs_correct_page_scores_and_no_question_text() -> None:
    rows = _trace(retrieved=[
        {"doc_id": "contract", "page_no": 8},
        {"doc_id": "contract", "page_no": 8},
        {"doc_id": "contract", "page_no": 7},
    ])
    report = evaluate_trace_bytes(_bytes(rows))
    assert report["case_total"] == 1
    assert report["duplicate_ranked_pages_removed"] == 1
    assert report["metrics"]["evidence_hit_rate"] == 1.0
    assert report["metrics"]["mrr_at_10"] == 0.5
    assert report["metrics"]["ndcg_at_10"] < 1.0
    assert report["embedding_version"] == "frozen-model-digest-v1"
    assert report["evidence_kind"] == "offline_recorded_ranks_no_model_calls"
    assert "warranty?" not in json.dumps(report).lower()
    assert report == evaluate_trace_bytes(_bytes(rows))


def test_wrong_page_cannot_claim_an_answer_bearing_hit() -> None:
    rows = _trace(retrieved=[{"doc_id": "contract", "page_no": 8}])
    metrics = evaluate_trace_bytes(_bytes(rows))["metrics"]
    assert metrics["evidence_hit_rate"] == 0.0
    assert metrics["recall_at_50"] == 0.0
    assert metrics["ndcg_at_10"] == 0.0


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[1].update({"split": "dev"}),
    lambda rows: rows[1].update({"gold": [
        {"doc_id": "contract", "page_no": 7, "grade": 1}
    ]}),
    lambda rows: rows[1].update({"retrieved": [
        {"doc_id": "contract", "page_no": 0}
    ]}),
    lambda rows: rows[0].update({"extra": True}),
    lambda rows: rows[0].update({"schema_version": 2}),
    lambda rows: rows[1].update({"case_id": "not-uuid"}),
    lambda rows: rows[1].update({"gold": [
        {"doc_id": "contract", "page_no": 7, "grade": 3},
        {"doc_id": "contract", "page_no": 7, "grade": 1},
    ]}),
])
def test_invalid_rank_trace_fails_closed(mutation) -> None:
    rows = _trace()
    mutation(rows)
    with pytest.raises(ValueError):
        evaluate_trace_bytes(_bytes(rows))


def test_duplicate_case_id_and_missing_manifest_are_rejected() -> None:
    rows = _trace()
    with pytest.raises(ValueError, match="duplicate case_id"):
        evaluate_trace_bytes(_bytes([*rows, rows[1]]))
    with pytest.raises(ValueError):
        evaluate_trace_bytes(_bytes(rows[1:]))


def test_cli_emits_deterministic_report_without_echoing_question(tmp_path, capsys) -> None:
    src = tmp_path / "trace.jsonl"
    dest = tmp_path / "scores.json"
    src.write_bytes(_bytes(_trace()))
    assert main([str(src), "--output", str(dest)]) == 0
    report = json.loads(dest.read_text())
    assert report["case_total"] == 1
    assert report["trace_sha256"] == evaluate_trace_bytes(src.read_bytes())["trace_sha256"]
    assert "What is the contract warranty?" not in dest.read_text()
    assert main([str(src), "--output", str(dest)]) == 0
    assert json.loads(dest.read_text()) == report
    assert capsys.readouterr().err == ""


def test_eval_service_refuses_swapped_case_ids_or_split_masquerade() -> None:
    cases = [
        EvalCase(id=uuid4(), tenant_id=uuid4(), question="q1", split="holdout"),
        EvalCase(id=uuid4(), tenant_id=uuid4(), question="q2", split="holdout"),
    ]
    outcomes = [
        CaseOutcome(case_id=cases[1].id, split="holdout"),
        CaseOutcome(case_id=cases[0].id, split="holdout"),
    ]
    with pytest.raises(ValidationError, match="case_id"):
        run_eval(
            tenant_scope_key="unit-only", bundle=default_bundle(),
            cases=cases, outcomes=outcomes, split="holdout",
        )
    aligned = [
        CaseOutcome(case_id=case.id, split="dev") for case in cases
    ]
    with pytest.raises(ValidationError, match="split"):
        run_eval(
            tenant_scope_key="unit-only", bundle=default_bundle(),
            cases=cases, outcomes=aligned, split="holdout",
        )
