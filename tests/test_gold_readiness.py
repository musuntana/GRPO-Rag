"""Offline-only gold preflight regressions (no model, DB or network)."""

import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_gold_readiness",
    Path(__file__).resolve().parents[1] / "scripts" / "check_gold_readiness.py",
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def rows():
    values = []
    for i in range(100):
        index = i % 50
        intent = "common" if index < 25 else "long_tail" if index < 40 else "multi_hop"
        values.append({
            "case_id": f"case-{i}", "split": "dev" if i < 50 else "holdout",
            "intent": intent, "reviewed_by": "human-reviewer",
            "source_version": "source-v1",
            "gold": [{"doc_id": "doc", "page_no": 1, "grade": 3}],
            "question": f"private warranty text {i}",
        })
    return values


def encode(items):
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def test_valid_split_balance_and_no_question_content():
    report = module.inspect_dataset(encode(rows()))
    assert report["ready"]
    assert report["case_count"] == 100
    assert report["split_intent_counts"]["holdout"] == {
        "common": 25, "long_tail": 15, "multi_hop": 10
    }
    assert "private warranty" not in json.dumps(report)


def test_cross_split_duplicates_fail():
    examples = rows()
    examples[50]["case_id"] = examples[0]["case_id"]
    examples[51]["question"] = "  PRIVATE WARRANTY  text 0 "
    report = module.inspect_dataset(encode(examples))
    assert not report["ready"]
    assert any("duplicate case_id" in issue for issue in report["issues"])
    assert any("leaked across splits" in issue for issue in report["issues"])


def test_bool_grade_and_duplicate_gold_rejected():
    examples = rows()
    examples[0]["gold"][0]["grade"] = True
    examples[1]["gold"] *= 2
    report = module.inspect_dataset(encode(examples))
    assert not report["ready"]
    assert any("invalid gold" in issue for issue in report["issues"])
    assert any("duplicate gold" in issue for issue in report["issues"])


def test_allows_grade_two_if_answer_bearing_three_present():
    examples = rows()
    examples[0]["gold"].append({"doc_id": "other", "page_no": 2, "grade": 2})
    assert module.inspect_dataset(encode(examples))["ready"]


def test_rejects_missing_review_and_unbalanced_holdout():
    examples = rows()
    examples[0]["reviewed_by"] = ""
    for item in examples[50:]:
        item["intent"] = "common"
    report = module.inspect_dataset(encode(examples))
    assert not report["ready"]
    assert any("reviewer" in issue for issue in report["issues"])
    assert any("holdout: intent" in issue for issue in report["issues"])


def test_rejects_duplicate_json_keys_and_missing_positive():
    text = encode(rows()).decode()
    lines = text.splitlines()
    lines[0] = lines[0].replace('"case_id":', '"case_id": "overwritten", "case_id":', 1)
    lines[1] = lines[1].replace('"grade": 3', '"grade": 1')
    report = module.inspect_dataset(("\n".join(lines) + "\n").encode())
    assert not report["ready"]
    assert any("ambiguous" in issue for issue in report["issues"])
    assert any("grade-3" in issue for issue in report["issues"])


def test_cli_refuses_overwrite_and_does_not_print_questions(tmp_path, capsys):
    path = tmp_path / "data.jsonl"
    path.write_bytes(encode(rows()))
    report_path = tmp_path / "report.json"
    assert module.main([str(path), "--output", str(report_path)]) == 0
    assert "private warranty" not in report_path.read_text()
    assert module.main([str(path), "--output", str(report_path)]) == 2
    assert "private warranty" not in capsys.readouterr().err


def test_unhashable_split_and_intent_rejected_without_crash():
    examples = rows()
    examples[0]["split"] = ["dev"]
    examples[1]["intent"] = {"unexpected": "object"}
    report = module.inspect_dataset(encode(examples))
    assert not report["ready"]
    assert any("invalid split" in issue for issue in report["issues"])
    assert any("invalid intent" in issue for issue in report["issues"])
