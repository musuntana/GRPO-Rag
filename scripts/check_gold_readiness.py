"""Read-only preflight for a human-reviewed page-level evaluation dataset.

This intentionally does not load models or inspect private question text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


ALLOWED_SPLITS = {"train", "dev", "holdout"}
ALLOWED_INTENTS = {"common", "long_tail", "multi_hop"}


def inspect_dataset(data: bytes) -> dict:
    """Validate JSONL metadata; fail closed on missing or ambiguous provenance."""
    counts: Counter[str] = Counter()
    splits: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    issues: list[str] = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            issues.append(f"line {number}: invalid JSON")
            continue
        if not isinstance(item, dict):
            issues.append(f"line {number}: expected object")
            continue
        case_id = item.get("case_id")
        split = item.get("split")
        intent = item.get("intent")
        if not isinstance(case_id, str) or not case_id.strip():
            issues.append(f"line {number}: missing case_id")
        if split not in ALLOWED_SPLITS:
            issues.append(f"line {number}: invalid split")
        if intent not in ALLOWED_INTENTS:
            issues.append(f"line {number}: invalid intent")
        if not isinstance(item.get("reviewed_by"), str) or not item["reviewed_by"].strip():
            issues.append(f"line {number}: missing human reviewer")
        if not isinstance(item.get("source_version"), str) or not item["source_version"].strip():
            issues.append(f"line {number}: missing source version")
        gold = item.get("gold")
        if not isinstance(gold, list) or not gold:
            issues.append(f"line {number}: missing gold labels")
        else:
            for entry in gold:
                if not isinstance(entry, dict) or not isinstance(entry.get("doc_id"), str) or (
                    not isinstance(entry.get("page_no"), int)
                    or isinstance(entry.get("page_no"), bool)
                    or entry["page_no"] < 1
                ) or entry.get("grade") not in (0, 1, 3):
                    issues.append(f"line {number}: invalid gold label")
                    break
        if isinstance(case_id, str) and case_id.strip():
            if any(old_id == case_id for old_id, _ in seen):
                issues.append(f"line {number}: duplicate or cross-split case_id")
            seen.add((case_id, str(split)))
        if intent in ALLOWED_INTENTS:
            counts[intent] += 1
        if split in ALLOWED_SPLITS:
            splits[split] += 1
    total = sum(counts.values())
    if total < 100:
        issues.append("fewer than 100 categorized cases")
    expected = {"common": 0.5, "long_tail": 0.3, "multi_hop": 0.2}
    if total and any(abs(counts[key] / total - target) > 0.05
                     for key, target in expected.items()):
        issues.append("intent distribution differs from 50/30/20 by over 5 percentage points")
    if not splits["dev"] or not splits["holdout"]:
        issues.append("dev and holdout splits required")
    return {
        "schema": "gold-readiness-v1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "case_count": total,
        "intent_counts": dict(sorted(counts.items())),
        "split_counts": dict(sorted(splits.items())),
        "ready": not issues,
        "issues": issues,
        "evidence_kind": "metadata_only_no_model_calls",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = inspect_dataset(args.dataset.read_bytes())
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
