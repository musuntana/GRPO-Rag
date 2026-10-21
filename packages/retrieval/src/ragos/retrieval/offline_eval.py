"""Versioned, deterministic OFFLINE evaluation of recorded page-level rankings.

This evaluates previously captured retrieval traces. It does not execute a
Qwen model, connect to Qdrant or validate citation semantics. Keeping this
boundary explicit prevents fixture metrics being reported as production gains.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from ragos.retrieval.metrics import Grade, evaluate_retrieval, unique_evidence
from ragos.retrieval.types import EvalCase, GoldEvidence

MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_CASES = 5000
MAX_GOLD = 50
MAX_RANKED = 500
FORMAT_VERSION = "page-ranking-trace-v1"
METRICS_VERSION = "page-aware-dedup-mrr10-v2"
_MANIFEST_KEYS = {
    "type", "schema_version", "dataset_id", "tenant_id", "split", "index_id",
    "embedding_profile", "embedding_version", "retriever_version",
}
_CASE_KEYS = {"type", "case_id", "split", "question", "gold", "retrieved"}
_GOLD_KEYS = {"doc_id", "page_no", "grade"}
_RANK_KEYS = {"doc_id", "page_no"}


def _object(value: Any, *, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} must have exactly the frozen schema fields")
    return value


def _name(value: Any, *, label: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} is required and must be nonempty")
    return value


def _uuid(value: Any, *, label: str) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        return UUID(value)
    except ValueError:
        raise ValueError(f"{label} must be a UUID") from None


def _page(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("page_no must be a positive integer")
    return value


def _gold(values: Any) -> list[GoldEvidence]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_GOLD:
        raise ValueError("gold must contain 1-50 page labels")
    labels: list[GoldEvidence] = []
    seen: set[tuple[str, int]] = set()
    for value in values:
        item = _object(value, fields=_GOLD_KEYS, label="gold label")
        doc_id = _name(item["doc_id"], label="gold doc_id")
        page_no = _page(item["page_no"])
        grade = item["grade"]
        if type(grade) is not int or grade not in (0, 1, 2, 3):
            raise ValueError("gold grade must be 0-3")
        key = (doc_id, page_no)
        if key in seen:
            raise ValueError("gold contains duplicate page identity")
        seen.add(key)
        labels.append(GoldEvidence(doc_id=doc_id, page_no=page_no, grade=grade))
    if not any(item.grade >= 3 for item in labels):
        raise ValueError("answerable page-ranking trace requires grade-3 gold")
    return labels


def _ranked(values: Any) -> list[Grade]:
    if not isinstance(values, list) or len(values) > MAX_RANKED:
        raise ValueError("retrieved must contain at most 500 page identities")
    ranks: list[Grade] = []
    for value in values:
        item = _object(value, fields=_RANK_KEYS, label="retrieved page")
        ranks.append(
            Grade(
                doc_id=_name(item["doc_id"], label="retrieved doc_id"),
                page_no=_page(item["page_no"]),
                grade=0,  # Only human/reference gold labels define relevance.
            )
        )
    return ranks


def evaluate_trace_bytes(raw: bytes) -> dict[str, Any]:
    """Score an answerable-only page ranking trace; fail on malformed input."""
    if not raw or len(raw) > MAX_TRACE_BYTES:
        raise ValueError("trace must be nonempty and at most 16 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("trace must be UTF-8") from None
    lines = text.splitlines()
    if len(lines) < 2 or any(not line.strip() for line in lines):
        raise ValueError("trace requires a manifest and at least one nonblank case")
    if len(lines) - 1 > MAX_CASES:
        raise ValueError("too many evaluation cases")
    try:
        manifest = _object(json.loads(lines[0]), fields=_MANIFEST_KEYS, label="manifest")
    except json.JSONDecodeError:
        raise ValueError("invalid manifest JSON") from None
    if manifest["type"] != "manifest" or type(manifest["schema_version"]) is not int:
        raise ValueError("invalid manifest schema")
    if manifest["schema_version"] != 1:
        raise ValueError("unsupported trace schema version")
    if manifest["split"] not in ("dev", "holdout"):
        raise ValueError("manifest split must be dev or holdout")
    metadata = {
        key: _name(manifest[key], label=key)
        for key in ("dataset_id", "embedding_profile", "embedding_version", "retriever_version")
    }
    tenant_id = _uuid(manifest["tenant_id"], label="tenant_id")
    index_id = _uuid(manifest["index_id"], label="index_id")
    cases: list[EvalCase] = []
    ranks: list[list[Grade]] = []
    case_ids: set[UUID] = set()
    duplicate_chunks = 0
    for line_number, line in enumerate(lines[1:], start=2):
        try:
            record = _object(json.loads(line), fields=_CASE_KEYS, label="trace case")
        except json.JSONDecodeError:
            raise ValueError(f"invalid case JSON at line {line_number}") from None
        if record["type"] != "case" or record["split"] != manifest["split"]:
            raise ValueError(f"case split/type mismatch at line {line_number}")
        case_id = _uuid(record["case_id"], label="case_id")
        if case_id in case_ids:
            raise ValueError("duplicate case_id in ranking trace")
        case_ids.add(case_id)
        question = _name(record["question"], label="question", limit=4000)
        gold = _gold(record["gold"])
        retrieved = _ranked(record["retrieved"])
        duplicate_chunks += len(retrieved) - len(unique_evidence(retrieved))
        cases.append(
            EvalCase(
                id=case_id,
                tenant_id=tenant_id,
                question=question,
                gold_evidence=gold,
                answerable=True,
                split=manifest["split"],
            )
        )
        ranks.append(retrieved)
    report = {
        "trace_format": FORMAT_VERSION,
        "metric_semantics": METRICS_VERSION,
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
        "split": manifest["split"],
        "dataset_id": metadata["dataset_id"],
        "tenant_id": str(tenant_id),
        "index_id": str(index_id),
        "embedding_profile": metadata["embedding_profile"],
        "embedding_version": metadata["embedding_version"],
        "retriever_version": metadata["retriever_version"],
        "case_total": len(cases),
        "duplicate_ranked_pages_removed": duplicate_chunks,
        "metrics": evaluate_retrieval(cases, ranks).as_dict(),
        "evidence_kind": "offline_recorded_ranks_no_model_calls",
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score frozen page-level ranking traces without executing a model"
    )
    parser.add_argument("trace", type=Path, help="local JSONL trace with first-line manifest")
    parser.add_argument("--output", type=Path, help="optional deterministic JSON result")
    args = parser.parse_args(argv)
    try:
        if args.trace.stat().st_size > MAX_TRACE_BYTES:
            raise ValueError("trace exceeds maximum size")
        report = evaluate_trace_bytes(args.trace.read_bytes())
        encoded = json.dumps(
            report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n"
        if args.output is not None:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
    except (OSError, ValueError) as exc:
        # Validation errors use fixed wording without echoing private source content.
        print(f"Trace validation failed: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
