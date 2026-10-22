"""Paired, leak-resistant page-reranker evaluation and graded training-pair mining.

Offline only. Both operations first validate the frozen v1 trace contract; they
never invoke models or infer relevance for unlabelled retrieved pages.
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

from ragos.retrieval.metrics import Grade, hit_at_k, unique_evidence
from ragos.retrieval.offline_eval import evaluate_trace_bytes
from ragos.retrieval.types import GoldEvidence

_FROZEN_FIELDS = (
    "dataset_id", "tenant_id", "split", "index_id",
    "embedding_profile", "embedding_version",
)


def _load(raw: bytes) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Delegate strict schema, bounds, UUIDs and grade checks to one evaluator."""
    report = evaluate_trace_bytes(raw)
    lines = raw.decode("utf-8").splitlines()
    cases = {record["case_id"]: record for record in map(json.loads, lines[1:])}
    return report, cases


def _units(case: dict[str, Any]) -> list[tuple[str, int]]:
    """Deduplicate chunks of the same PDF page, preserving first-hit rank."""
    return [(str(g.doc_id), g.page_no) for g in unique_evidence(
        Grade(doc_id=item["doc_id"], page_no=item["page_no"], grade=0)
        for item in case["retrieved"]
    )]


def compare_page_traces(baseline_raw: bytes, candidate_raw: bytes) -> dict[str, Any]:
    """Compare two rankings of the exact same candidate pages and frozen gold.

    Only a reranking experiment may use this function: recall/source changes
    require a *separate* retrieval evaluation rather than attributing their
    effects to the ranker.
    """
    baseline, first = _load(baseline_raw)
    candidate, second = _load(candidate_raw)
    if (
        baseline["trace_format"] != "page-ranking-trace-v1"
        or candidate["trace_format"] != "page-ranking-trace-v1"
    ):
        raise ValueError("paired ranking evaluation requires dev/holdout v1, never train-v2")
    if any(baseline[key] != candidate[key] for key in _FROZEN_FIELDS):
        raise ValueError("paired traces must share frozen dataset, index, model and split")
    if first.keys() != second.keys():
        raise ValueError("paired traces have different case IDs")

    wins = losses = ties = 0
    for case_id, prior in first.items():
        next_case = second[case_id]
        # Gold page order does not change its labels, but edits to its grades do.
        def labels(case: dict[str, Any]) -> list[tuple[str, int, int]]:
            return sorted((g["doc_id"], g["page_no"], g["grade"]) for g in case["gold"])

        if prior["question"] != next_case["question"] or labels(prior) != labels(next_case):
            raise ValueError("paired case question or graded gold changed")
        prior_units, next_units = _units(prior), _units(next_case)
        # A reranker may permute chunks, never silently add/remove them.
        # Unique page-set equality alone misses deleted duplicate child chunks
        # on the same PDF page and can distort page-level rank positions.
        prior_chunks = Counter(
            (item["doc_id"], item["page_no"]) for item in prior["retrieved"]
        )
        next_chunks = Counter(
            (item["doc_id"], item["page_no"]) for item in next_case["retrieved"]
        )
        if prior_chunks != next_chunks:
            raise ValueError("paired reranker changed recalled page/chunk multiplicity")
        if set(prior_units) != set(next_units):
            raise ValueError("paired reranker changed the recalled page universe")
        gold = [GoldEvidence(**item) for item in prior["gold"]]
        before = hit_at_k([Grade(doc_id=d, page_no=p, grade=0) for d, p in prior_units], gold, 6)
        after = hit_at_k([Grade(doc_id=d, page_no=p, grade=0) for d, p in next_units], gold, 6)
        if after > before:
            wins += 1
        elif after < before:
            losses += 1
        else:
            ties += 1

    return {
        "evidence_kind": "offline_paired_rerank_traces_no_model_calls",
        "metric_semantics": baseline["metric_semantics"],
        "split": baseline["split"],
        "dataset_id": baseline["dataset_id"],
        "index_id": baseline["index_id"],
        "embedding_profile": baseline["embedding_profile"],
        "embedding_version": baseline["embedding_version"],
        "case_total": len(first),
        "baseline": {
            "trace_sha256": baseline["trace_sha256"],
            "retriever_version": baseline["retriever_version"],
            "metrics": baseline["metrics"],
        },
        "candidate": {
            "trace_sha256": candidate["trace_sha256"],
            "retriever_version": candidate["retriever_version"],
            "metrics": candidate["metrics"],
        },
        "delta_candidate_minus_baseline": {
            key: candidate["metrics"][key] - before
            for key, before in baseline["metrics"].items()
        },
        "hit_at_6_case_outcomes": {"improved": wins, "regressed": losses, "tied": ties},
        "label_review_status": "not_attested_by_trace_v1",
    }


def mine_trace_pairs(raw: bytes, *, window: int = 50, related_cap: int = 4,
                     irrelevant_cap: int = 2) -> dict[str, Any]:
    """Select *explicitly graded*, actually recalled train-v2 examples only.

    Relevance of an unlabelled retrieved page is unknown, never grade zero.
    A positive that was not recalled is a recall failure, not ranker training.
    """
    if not 1 <= window <= 50 or not 0 <= related_cap <= 50 or not 0 <= irrelevant_cap <= 50:
        raise ValueError("invalid hard-negative rank window or per-positive caps")
    report, cases = _load(raw)
    if report["split"] != "train" or report["trace_format"] != "page-ranking-trace-train-v2":
        raise ValueError("mining requires an approved train-v2 trace, never dev/holdout")

    pairs: list[dict[str, Any]] = []
    missed_positive = 0
    unlabeled_pages = 0
    for case_id, case in sorted(cases.items()):
        ranked = _units(case)[:window]
        ranks = {unit: position for position, unit in enumerate(ranked, start=1)}
        labels = {(g["doc_id"], g["page_no"]): g["grade"] for g in case["gold"]}
        unlabeled_pages += sum(unit not in labels for unit in ranked)
        positives = sorted(
            (unit for unit, grade in labels.items() if grade == 3),
            key=lambda unit: (ranks.get(unit, 10**9), unit),
        )
        candidates = {
            grade: [unit for unit in ranked if labels.get(unit) == grade]
            for grade in (1, 0)
        }
        for positive in positives:
            if positive not in ranks:
                missed_positive += 1
                continue
            for grade, cap in ((1, related_cap), (0, irrelevant_cap)):
                for negative in candidates[grade][:cap]:
                    if negative == positive:
                        continue
                    reason = (
                        "same_document_adjacent"
                        if positive[0] == negative[0] and abs(positive[1] - negative[1]) == 1
                        else "same_document" if positive[0] == negative[0]
                        else "cross_document_related" if grade == 1
                        else "explicit_irrelevant"
                    )
                    pairs.append({
                        "case_id": case_id,
                        "question": case["question"],
                        "positive_doc_id": positive[0],
                        "positive_page_no": positive[1],
                        "positive_rank": ranks[positive],
                        "positive_grade": 3,
                        "negative_doc_id": negative[0],
                        "negative_page_no": negative[1],
                        "negative_rank": ranks[negative],
                        "negative_grade": grade,
                        "negative_reason": reason,
                    })

    return {
        "evidence_kind": "offline_explicitly_graded_pairs_not_training",
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
        "split": "train",
        "dataset_id": report["dataset_id"],
        "index_id": report["index_id"],
        "retriever_version": report["retriever_version"],
        "case_total": len(cases),
        "missed_positive_pages": missed_positive,
        "unlabeled_retrieved_pages_ignored": unlabeled_pages,
        "related_pairs": sum(item["negative_grade"] == 1 for item in pairs),
        "irrelevant_pairs": sum(item["negative_grade"] == 0 for item in pairs),
        "label_review_status": "not_attested_by_train_v2",
        "pairs": pairs,
    }


def _write_new(path: Path, report: dict[str, Any]) -> None:
    """No stdout of private prompts; never follow symlinks or overwrite files."""
    data = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict offline page-ranking experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="Paired, frozen-universe reranker A/B")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", required=True, type=Path)
    mine = commands.add_parser("mine", help="Mine labelled train-v2 negatives only")
    mine.add_argument("trace", type=Path)
    mine.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare_page_traces(args.baseline.read_bytes(), args.candidate.read_bytes())
        else:
            result = mine_trace_pairs(args.trace.read_bytes())
        _write_new(args.output, result)
    except (OSError, ValueError) as exc:
        print(f"Benchmark rejected: {exc.__class__.__name__} (input text suppressed)",
              file=sys.stderr)
        return 2
    print(f"Wrote offline {args.command} report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
