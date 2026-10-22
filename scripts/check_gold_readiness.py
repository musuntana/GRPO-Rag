"""Read-only metadata readiness gate for human-reviewed page-level gold JSONL.

This tool does not invoke a model, certify a reviewer's identity, or inspect
semantic correctness. No private question/source content appears in its report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
MAX_CASES = 5000
MAX_GOLD = 50
ALLOWED_SPLITS = frozenset({"train", "dev", "holdout"})
ALLOWED_INTENTS = frozenset({"common", "long_tail", "multi_hop"})
TARGET_MIX = {"common": 0.5, "long_tail": 0.3, "multi_hop": 0.2}


def _json_object_unique(raw: bytes) -> dict[str, Any]:
    """Reject ambiguous JSON keys instead of silently accepting last-wins."""

    def pairs_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs_unique)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mix_ok(counts: Counter[str], size: int, tolerance: float) -> bool:
    return bool(size) and all(
        abs(counts[intent] / size - fraction) <= tolerance + 1e-10
        for intent, fraction in TARGET_MIX.items()
    )


def inspect_dataset(data: bytes) -> dict[str, Any]:
    """Check metadata only; fail closed on unreviewed or leaking split records."""
    if not data or len(data) > MAX_BYTES:
        raise ValueError("dataset must be nonempty and at most 16 MiB")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ValueError("dataset must be UTF-8") from None
    if len(lines) > MAX_CASES:
        raise ValueError("too many cases")

    intent_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    split_intents: dict[str, Counter[str]] = {
        split: Counter() for split in ALLOWED_SPLITS
    }
    seen_ids: set[str] = set()
    seen_questions: dict[str, str] = {}
    issues: list[str] = []
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip():
            issues.append(f"line {line_no}: blank record")
            continue
        try:
            item = _json_object_unique(raw.encode("utf-8"))
        except ValueError:
            issues.append(f"line {line_no}: invalid or ambiguous JSON object")
            continue

        case_id = item.get("case_id")
        split = item.get("split")
        intent = item.get("intent")
        if not _present(case_id):
            issues.append(f"line {line_no}: missing case_id")
        elif case_id in seen_ids:
            issues.append(f"line {line_no}: duplicate case_id across dataset/splits")
        else:
            seen_ids.add(case_id)
        if not isinstance(split, str) or split not in ALLOWED_SPLITS:
            issues.append(f"line {line_no}: invalid split")
        else:
            split_counts[split] += 1
        if not isinstance(intent, str) or intent not in ALLOWED_INTENTS:
            issues.append(f"line {line_no}: invalid intent")
        else:
            intent_counts[intent] += 1
            if isinstance(split, str) and split in ALLOWED_SPLITS:
                split_intents[split][intent] += 1

        question = item.get("question")
        if not _present(question):
            issues.append(f"line {line_no}: missing question")
        else:
            # Hash comparison only: never emit the original prompt or fingerprint.
            fingerprint = hashlib.sha256(" ".join(question.casefold().split()).encode()).hexdigest()
            previous_split = seen_questions.setdefault(fingerprint, str(split))
            if previous_split != str(split):
                issues.append(f"line {line_no}: identical question leaked across splits")

        if not _present(item.get("reviewed_by")):
            issues.append(f"line {line_no}: missing human reviewer metadata")
        if not _present(item.get("source_version")):
            issues.append(f"line {line_no}: missing source version")

        gold = item.get("gold")
        if not isinstance(gold, list) or not 1 <= len(gold) <= MAX_GOLD:
            issues.append(f"line {line_no}: expected 1-50 gold labels")
            continue
        seen_pages: set[tuple[str, int]] = set()
        has_positive = False
        for entry in gold:
            if (
                not isinstance(entry, dict)
                or not _present(entry.get("doc_id"))
                or type(entry.get("page_no")) is not int
                or entry["page_no"] < 1
                or type(entry.get("grade")) is not int
                or entry["grade"] not in (0, 1, 2, 3)
            ):
                issues.append(f"line {line_no}: invalid gold label")
                break
            page = (entry["doc_id"], entry["page_no"])
            if page in seen_pages:
                issues.append(f"line {line_no}: duplicate gold page identity")
                break
            seen_pages.add(page)
            has_positive |= entry["grade"] == 3
        else:
            if not has_positive:
                issues.append(f"line {line_no}: answerable gold requires grade-3 evidence")

    total = len(lines)
    if total < 100:
        issues.append("fewer than 100 cases")
    if not _mix_ok(intent_counts, total, 0.05):
        issues.append("dataset intent distribution differs from 50/30/20 by over 5pp")
    for split in ("dev", "holdout"):
        if split_counts[split] < 10:
            issues.append(f"{split}: fewer than 10 cases")
        elif not _mix_ok(split_intents[split], split_counts[split], 0.10):
            issues.append(f"{split}: intent distribution differs from 50/30/20 by over 10pp")

    return {
        "schema": "gold-readiness-v1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "case_count": total,
        "intent_counts": dict(sorted(intent_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "split_intent_counts": {
            name: dict(sorted(split_intents[name].items()))
            for name in ("train", "dev", "holdout")
        },
        "ready": not issues,
        "issues": issues,
        "evidence_kind": "metadata_only_no_model_calls_no_label_semantics_attested",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.dataset.stat().st_size > MAX_BYTES:
            raise ValueError("dataset too large")
        report = inspect_dataset(args.dataset.read_bytes())
        rendered = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if args.output:
            # No overwrite or symlink following: private report may contain IDs.
            fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as writer:
                writer.write(rendered)
        else:
            sys.stdout.buffer.write(rendered)
    except (OSError, ValueError):
        print("Gold readiness failed: invalid input or output file", file=sys.stderr)
        return 2
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
