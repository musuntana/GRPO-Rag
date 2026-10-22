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

### P0/P1: capture paired traces from ONE source recall

`ragos.runtime.paired_trace_capture.capture_paired_page_traces` calls the
existing approved, single-index `capture_page_ranking_trace` twice through a
request-local, frozen cache: the first pass fetches actual PG-hydrated Dense
results, the second reuses **the exact same candidates**, scores only the
configured rerank depth, and appends the untouched tail. No second embedding
or vector query is performed for the same question.

The model callback receives only `(question, trusted candidate chunks,
tenant_ctx)`, **never** an `EvalCase`, gold labels or holdout outcomes.
It must be an authorized `RerankFn` supplied by the caller after model
admission and budget checks; this utility does not itself admit any model or
perform HTTP. The candidate must be a full permutation of the input head.
The bridge checks immutable candidate metadata against a pre-call snapshot
even if the underlying provider mutates input objects. Results are
`PairedPageTraces(baseline bytes, candidate bytes, content-free comparison)`.

```bash
.venv/bin/pytest -q tests/test_paired_trace_capture.py \
  tests/test_page_benchmark.py tests/test_page_trace_capture.py
.venv/bin/ruff check packages/runtime/src/ragos/runtime/paired_trace_capture.py \
  tests/test_paired_trace_capture.py
```

The test harness uses a deterministic in-memory fake Dense/Reranker transport;
it is not evidence of actual Qwen model ranking, published PostgreSQL index
readback or end-user citation quality. Actual source data must be captured by
an operator with **active** approved PG/Qdrant publication and gateway-bound
model callback. Raw returned baseline/candidate bytes contain questions and
human labels: store securely; do not print/commit them. The comparison can
be published only after reviewing dataset consent, deduplication and the
model/index/version manifest.

### Provider scoring through the existing tenant ModelRouter

The previous provider interface already defined `BaseProvider.rerank` and
`OpenAICompatibleProvider.rerank` for an **optional** /v1/reranks HTTP route.
This patch adds `ModelRouter.rerank(ctx, query, documents)` and the
`GatewayRerankAdapter(router)` callable, so an approved tenant
`use_case='rerank'` binding can supply the `RerankFn` consumed by
`capture_paired_page_traces(..., reranker_fn=GatewayRerankAdapter(router))`.
There is no new custom scoring algorithm or untrusted provider URL.

The router enforces a nonempty <=4000 character question, 1-50 nonempty
passages <=16k characters each and <=160k characters total, before resolving
the tenant's model binding; it validates a complete, non-repeated set of
finite scores indexed by the original request ordering. The async adapter
runs the synchronous gateway call outside the event loop, sorts scores
deterministically, and reconstructs `ScoredChunk` records solely from original
input evidence. The separate online reranker contract (#20) remains the
authoritative evidence integrity check and must be merged first.

**Not live Qwen:** existing local Docker Model Runner admission permits only
pinned Chat and Embedding; rerank use case deliberately fails closed.
The provider-specific /reranks route has a tested *fixture* HTTP contract,
but no verified Docker Model Runner support; do not set
`RAGOS_PROBE_RERANK_MODEL` merely because a Qwen3-Reranker weight exists.
An authorized production secret resolver, token/budget/cost enforcement,
and an operator-approved real scoring provider remain deployment blockers.
Tests in `tests/test_gateway_rerank.py` inject only a fake provider.

```bash
.venv/bin/pytest -q tests/test_gateway_rerank.py tests/test_paired_trace_capture.py
.venv/bin/ruff check packages/model-gateway/src/ragos/model_gateway/router.py \
  packages/retrieval/src/ragos/retrieval/gateway.py tests/test_gateway_rerank.py
```

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
- For the first model-backed baseline, prefer Qwen's documented
  `sentence_transformers.CrossEncoder('Qwen/Qwen3-Reranker-0.6B')`
  `predict(query_document_pairs)` and `rank(query, documents)` adapter,
  using pinned versions and the native logit-difference score; only use
  QwenLM's lower-level Transformers yes/no scoring when this higher-level
  mature path demonstrably fails or lacks needed features. Alternatively, use an explicitly
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

Official implementation references: [Qwen3-Reranker-0.6B model card / CrossEncoder usage](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) and [QwenLM Qwen3 Reranker](https://github.com/QwenLM/Qwen3-Embedding) and [Docker Model Runner API](https://docs.docker.com/ai/model-runner/api-reference/).
