# S1 page-ranking trace evaluation contract (2026-10-21)

This patch adds a **deterministic evaluator for recorded rankings**, not a new
online retriever and not a proof that a real Qwen model improved recall.

## Why this was necessary

- Qdrant commonly returns multiple child chunks from **the same PDF page**.
  The previous evaluator could score them as separate page-ranking entries;
  repeated gold entries could make NDCG exceed 1. The shared metrics module
  now deduplicates by `(doc_id, page_no)`, preserving first-hit rank.
- The published metric called `mrr_at_10` previously examined every rank.
  It now cuts off at **ten unique evidence pages**.
- The evaluation service now checks that case IDs, outcome IDs and explicit
  dev/holdout splits match; it cannot silently credit a swapped case or
  relabel a development result as holdout.

These changes alter historical retrieval metric semantics. **Recompute
previous reports**, including any earlier Hit@6, NDCG and MRR claims; do
not compare old and new numbers as equivalent measurements.

## Input: frozen JSONL ranking trace

File `examples/eval/synthetic-page-trace.jsonl` has a first-line manifest:

```json
{"type":"manifest","schema_version":1,"dataset_id":"synthetic-page-cases-v1","tenant_id":"00000000-0000-4000-8000-000000000100","split":"holdout","index_id":"00000000-0000-4000-8000-000000000101","embedding_profile":"synthetic_only","embedding_version":"synthetic-not-real-qwen","retriever_version":"fixture-ranking-v1"}
```

Each following line is one case, with a UUID `case_id`, a matching
`split`, nonblank `question`, nonduplicated **1-based** gold labels
`[{ "doc_id": "contract", "page_no": 7, "grade": 3 }]` and a ranked
list of `retrieved` page identities. `grade=3` means answer-bearing;
`grade=1` is related but not answer-bearing. Repeated child hits are
allowed in the recorded ranking input and deduplicated in scoring.

The v1 benchmark intentionally accepts **only answerable questions with
at least one grade-3 gold page**. Do not mix unanswerable questions into
Hit@6 averages; assess abstention separately. Inputs are checked for
malformed schema, duplicate case IDs/gold pages, invalid page numbers,
wrong splits, and excessive length. It never infers a page from doc ID.

No retrieved grade is trusted: only human/reference gold labels are used
to determine relevance, not a model's claimed confidence.

## Deterministic CLI

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/python -m ragos.retrieval.offline_eval \
  examples/eval/synthetic-page-trace.jsonl \
  --output /tmp/page-rank-report.json
.venv/bin/pytest -q tests/test_page_eval_baseline.py
```

Output: `trace_sha256` over exact source bytes, evaluation schema/version,
`tenant_id`, `index_id`, `embedding_profile`, `embedding_version`,
`retriever_version`, `split`, case count, duplicate pages removed and
seven metrics from the shared `evaluate_retrieval` module. It intentionally
does not print source questions, retrieved text, prompts or embeddings.
Reports for different splits, index/model identities or metric versions
are **not comparable** without explicit experiment-level controls.

This command **scores a provided trace**, not a live Qdrant run.
The synthetic example has 2 questions and is for schema/CLI validation only:
no live Qwen / Embedding / Reranker was invoked, no 1,200-question evaluation
was imported and no GRPO/LoRA optimization was performed.

## Next measurable integration gate

Capture actual Qdrant+PostgreSQL page-level rankings into this trace schema
from an **authorized** and version-frozen retrieval run; never synthesize
missing pages from gold labels. Capture model revision, source/index identity,
case splits, and evaluation-set digest, then run holdout scoring. Human-check
a sample of page-level gold labels and semantic citations. Only then compare
no-reranker, standard reranker and GRPO reranker runs with identical recall
and evaluation settings. The live producer and enterprise authorization/
secret resolver are intentionally **not** added by this offline evaluator.

Rollback: revert this PR. No database schema, credentials, tenant settings
or production vector collections are modified.
