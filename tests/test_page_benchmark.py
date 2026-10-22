"""Offline frozen reranker A/B and reviewed-label-only mining contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from ragos.retrieval.benchmark import compare_page_traces, main, mine_trace_pairs


def _rows(*, split: str = "dev") -> list[dict]:
    return [
        {
            "type": "manifest", "schema_version": 2 if split == "train" else 3,
            "dataset_id": "synthetic-paired-v1",
            "tenant_id": str(uuid4()), "index_id": str(uuid4()),
            "split": split, "embedding_profile": "synthetic_only",
            "embedding_version": "frozen-synthetic-digest",
            "retriever_version": "dense-v1",
        },
        {
            "type": "case", "case_id": str(uuid4()), "split": split,
            "question": "Where is the warranty?",
            "gold": [
                {"doc_id": "contract", "page_no": 7, "grade": 3},
                {"doc_id": "contract", "page_no": 8, "grade": 1},
                {"doc_id": "other", "page_no": 3, "grade": 0},
            ],
            "retrieved": [
                {
                    "doc_id": "contract", "page_no": 8,
                    **({"chunk_id": str(uuid4())} if split != "train" else {}),
                },
                {
                    "doc_id": "contract", "page_no": 8,
                    **({"chunk_id": str(uuid4())} if split != "train" else {}),
                },
                {
                    "doc_id": "other", "page_no": 3,
                    **({"chunk_id": str(uuid4())} if split != "train" else {}),
                },
                {
                    "doc_id": "contract", "page_no": 7,
                    **({"chunk_id": str(uuid4())} if split != "train" else {}),
                },
                {
                    "doc_id": "unknown", "page_no": 11,
                    **({"chunk_id": str(uuid4())} if split != "train" else {}),
                },
            ],
        },
    ]


def _raw(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n").encode()


def test_paired_rerank_measures_same_universe_and_reports_no_text() -> None:
    base = _rows()
    improved = deepcopy(base)
    improved[0]["retriever_version"] = "rerank-v1"
    improved[1]["retrieved"] = [
        improved[1]["retrieved"][3], *improved[1]["retrieved"][:3],
        improved[1]["retrieved"][4],
    ]
    result = compare_page_traces(_raw(base), _raw(improved))
    assert result["case_total"] == 1
    assert result["candidate"]["metrics"]["mrr_at_10"] == 1.0
    assert result["delta_candidate_minus_baseline"]["ndcg_at_10"] > 0
    assert result["hit_at_6_case_outcomes"]["tied"] == 1
    assert "Where is the warranty?" not in json.dumps(result)
    assert result["label_review_status"] == "not_attested_by_trace_v3"
    assert result["candidate_identity_contract"] == "exact_chunk_id_and_page_v3"


@pytest.mark.parametrize("edit", [
    lambda rows: rows[0].update({"index_id": str(uuid4())}),
    lambda rows: rows[0].update({"embedding_version": "another-version"}),
    lambda rows: rows[1].update({"question": "swapped?"}),
    lambda rows: rows[1]["gold"][1].update({"grade": 2}),
    lambda rows: rows[1]["retrieved"].pop(),
    lambda rows: rows[1]["retrieved"].pop(1),  # same unique pages, wrong child count
    lambda rows: rows[1]["retrieved"][0].update({"doc_id": "foreign"}),
    # Same page coordinates but a different chunk must not pass a paired comparison.
    lambda rows: rows[1]["retrieved"][0].update({"chunk_id": str(uuid4())}),
    lambda rows: rows[1]["retrieved"][1].update({
        "chunk_id": rows[1]["retrieved"][0]["chunk_id"]
    }),
    lambda rows: rows[1].update({"case_id": str(uuid4())}),
])
def test_comparison_refuses_identity_gold_or_candidate_change(edit) -> None:
    base = _rows()
    candidate = deepcopy(base)
    edit(candidate)
    with pytest.raises(ValueError):
        compare_page_traces(_raw(base), _raw(candidate))


def test_mining_requires_graded_retrieved_negatives_and_recalled_positive() -> None:
    result = mine_trace_pairs(_raw(_rows(split="train")))
    assert len(result["pairs"]) == 2
    assert [p["negative_grade"] for p in result["pairs"]] == [1, 0]
    assert result["pairs"][0]["negative_reason"] == "same_document_adjacent"
    assert result["pairs"][0]["negative_rank"] == 1
    assert result["pairs"][0]["positive_rank"] == 3
    assert result["unlabeled_retrieved_pages_ignored"] == 1
    assert result["label_review_status"] == "not_attested_by_train_v2"
    assert all(p["negative_doc_id"] != "unknown" for p in result["pairs"])


def test_unrecalled_gold_positive_is_not_used_for_ranker_training() -> None:
    rows = _rows(split="train")
    rows[1]["retrieved"] = rows[1]["retrieved"][:3]
    result = mine_trace_pairs(_raw(rows))
    assert result["pairs"] == []
    assert result["missed_positive_pages"] == 1


def test_holdout_cannot_be_mined_and_caps_are_checked() -> None:
    with pytest.raises(ValueError, match="train-v2"):
        mine_trace_pairs(_raw(_rows(split="holdout")))
    with pytest.raises(ValueError, match="train-v2"):
        mine_trace_pairs(_raw(_rows(split="dev")))
    with pytest.raises(ValueError, match="invalid hard-negative"):
        mine_trace_pairs(_raw(_rows(split="train")), window=51)


def test_cli_writes_private_non_overwriting_json(tmp_path: Path) -> None:
    source = tmp_path / "dev.jsonl"
    output = tmp_path / "pairs.json"
    source.write_bytes(_raw(_rows(split="train")))
    assert main(["mine", str(source), "--output", str(output)]) == 0
    assert len(json.loads(output.read_text())["pairs"]) == 2
    assert output.stat().st_mode & 0o777 == 0o600
    assert main(["mine", str(source), "--output", str(output)]) == 2


def test_train_v2_is_separate_from_dev_holdout_v3() -> None:
    from ragos.retrieval.offline_eval import evaluate_trace_bytes

    train = _rows(split="train")
    report = evaluate_trace_bytes(_raw(train))
    assert report["trace_format"] == "page-ranking-trace-train-v2"
    assert report["split"] == "train"
    train[0]["schema_version"] = 1
    with pytest.raises(ValueError, match="v1"):
        evaluate_trace_bytes(_raw(train))
    holdout = _rows(split="holdout")
    assert holdout[0]["schema_version"] == 3
    holdout[0]["schema_version"] = 2
    with pytest.raises(ValueError, match="v2"):
        evaluate_trace_bytes(_raw(holdout))


def test_training_trace_cannot_be_used_as_reranker_quality_benchmark() -> None:
    rows = _rows(split="train")
    with pytest.raises(ValueError, match="page-ranking-trace-v3"):
        compare_page_traces(_raw(rows), _raw(rows))


def test_paired_rerank_allows_chunk_reorder_when_identity_multiset_matches() -> None:
    rows = _rows()
    ranked = deepcopy(rows)
    ranked[0]["retriever_version"] = "reranked-v1"
    ranked[1]["retrieved"] = list(reversed(ranked[1]["retrieved"]))
    result = compare_page_traces(_raw(rows), _raw(ranked))
    assert result["case_total"] == 1
    assert result["baseline"]["metrics"] != result["candidate"]["metrics"]
