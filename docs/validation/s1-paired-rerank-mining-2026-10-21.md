# S1 P0/P1: paired page rerank benchmark and graded-negative export (2026-10-21)

## Purpose and scope

This increment adds deterministic **offline** experiment handling after the already
implemented authorized PG/Qdrant page-trace capture. Approved train cases
are captured with a separate v2 manifest; existing dev/holdout v1 files
remain unchanged. It does not connect the user's
Mac Docker Model Runner, run actual Qwen weights, provide customer gold labels,
train an SFT/GRPO policy, or attest citations. It does not change database schemas
or CI triggers.

### P0: compare Dense baseline and reranker fairly

`ragos.retrieval.benchmark.compare_page_traces` validates each file using the
existing frozen `page-ranking-trace-v1` evaluator, then fails closed unless:
- dataset, tenant, split, published index, embedding profile and revision agree;
- case IDs, **actual question text**, page-level gold labels and grades agree;
- both retrieval runs contain precisely the same set of unique
  `(doc_id,page_no)` candidates for every case (ranking may change; recall may not).

Only `retriever_version` and candidate rank order may change. This isolates the
effect of **reranking** from recall/index drift. It returns the existing shared
metrics (Recall@5/10/50, Precision@10, NDCG@10, MRR@10, Hit@6), their deltas and
per-question Hit@6 win/loss/tie counts. The report contains trace SHA-256, not
questions or source document text. `trace-v1` alone cannot attest that gold was
human-reviewed: the report explicitly marks this caveat.

Run on a developer host with locally authorized private traces:

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/python -m ragos.retrieval.benchmark compare \
  /secure/dev-dense.jsonl /secure/dev-reranker.jsonl \
  --output /secure/dev-comparison.json
```

A reranked trace must be captured using the **same original source candidate
pages** and the existing schema, not invented by sorting according to gold.
Results from different embedding revisions or source index versions cannot
be attributed to reranking. Before promotion, run the same fully frozen
comparison on a separately reviewed holdout split without tuning to it.

### P1: mine *labelled and recalled* hard negative pairs

The `mine` mode accepts **train-v2 only**. The v1 schema remains
restricted to dev/holdout, while approved authorized train cases use v2. It scores the actual de-duplicated first
50 pages in each question, emits positive grade-3 + negative grade-1/0 pairs
only if both pages were explicitly labelled *and* the positive was recalled.
Unlabelled retrieved pages and grade-2 ambiguous pages **never become negatives**.
Separate per-positive caps (four grade-1, two grade-0) prevent excessive related
negatives. The report counts misses and unknown labels rather than hiding them.
Dev/holdout cases can never become training pairs.

```bash
.venv/bin/python -m ragos.retrieval.benchmark mine \
  /secure/train-dense-v2.jsonl --output /secure/train-graded-pairs.json
.venv/bin/pytest -q tests/test_page_benchmark.py tests/test_page_eval_baseline.py \
  tests/test_page_trace_capture.py
.venv/bin/ruff check packages/retrieval/src/ragos/retrieval/benchmark.py \
  tests/test_page_benchmark.py
```

**Privacy:** Pair exports contain real *questions*, IDs and labelled page references;
keep input/output in approved private storage, never commit them, and do not
expose them in Actions logs. Output is created as a new file with 0600 permissions
and refuses overwrites/symlinks. Console prints only completion or a redacted error.
The export is not a model checkpoint or training run.

**Review gap:** A v1/v2 trace carries a grade but no independently verifiable
reviewer signature. Operator must separately audit annotation provenance,
source document consent, per-tenant ACL and train/dev/holdout near-duplicate leakage
before using exported pairs for model training. A zero-negative export means
label coverage is insufficient, **not** that a model has zero errors.

### Serving / model limitations

- PR #20 separately hardens `rerank()` to ensure exact candidate permutation
  and immutable source lineage; this offline benchmark does not replace it.
- Docker Model Runner's currently documented OpenAI-compatible routes cover
  `/chat/completions` and `/embeddings`, not a standardized `/rerank`.
  Do not assume the locally installed `qwen3-reranker` exposes a score API.
- For a baseline choose a genuinely tested Qwen3-Reranker-0.6B implementation
  using QwenLM's official Transformers yes/no scoring example or an explicitly
  compatible serving adapter, then put model calls behind the model gateway.
  Model weight identity, tokenizer, instruction, precision, batch size, CPU/GPU
  device, latency and request budget must be frozen alongside the trace.
- GRPO/LoRA training must remain gated on demonstrable improvements over
  the mature reranker and clean human-reviewed data.

## Rollback and evidence

Revert this PR to remove the optional CLI, tests and train-v2 support; existing
v1 dev/holdout semantics remain unchanged. Nothing is
written to existing PG rows, Qdrant collections, secrets or model artifacts.
Targeted tests and one manual (not push-triggered) Quick check must succeed
on the exact final PR HEAD before merging. `main` must not be called verified
from an earlier PR's CI run.

Official implementation references: [QwenLM Qwen3 Reranker](https://github.com/QwenLM/Qwen3-Embedding) and [Docker Model Runner API](https://docs.docker.com/ai/model-runner/api-reference/).
