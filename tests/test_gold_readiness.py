"""Offline, no-provider smoke checks for the gold readiness preflight."""
import importlib.util
from pathlib import Path
import json

SPEC = importlib.util.spec_from_file_location(
    "check_gold_readiness",
    Path(__file__).resolve().parents[1] / "scripts" / "check_gold_readiness.py",
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_rows():
    rows = []
    for i in range(100):
        intent = "common" if i < 50 else "long_tail" if i < 80 else "multi_hop"
        rows.append({
            "case_id": f"case-{i}", "split": "dev" if i < 80 else "holdout",
            "intent": intent, "reviewed_by": "human-reviewer",
            "source_version": "source-v1",
            "gold": [{"doc_id": "doc", "page_no": 1, "grade": 3}],
            "question": "private text should not appear in report",
        })
    return rows


def encode(rows):
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()


def test_valid_distribution_and_redaction():
    report = module.inspect_dataset(encode(make_rows()))
    assert report["ready"]
    assert report["case_count"] == 100
    assert "private text" not in json.dumps(report)


def test_rejects_cross_split_duplicate_and_unreviewed():
    rows = make_rows()
    rows[-1]["case_id"] = rows[0]["case_id"]
    rows[1]["reviewed_by"] = ""
    report = module.inspect_dataset(encode(rows))
    assert not report["ready"]
    assert any("duplicate" in issue for issue in report["issues"])
    assert any("reviewer" in issue for issue in report["issues"])


def test_rejects_invalid_page_and_missing_holdout():
    rows = make_rows()
    for row in rows:
        row["split"] = "dev"
    rows[0]["gold"][0]["page_no"] = 0
    report = module.inspect_dataset(encode(rows))
    assert not report["ready"]
    assert any("gold" in issue for issue in report["issues"])
    assert any("holdout" in issue for issue in report["issues"])
