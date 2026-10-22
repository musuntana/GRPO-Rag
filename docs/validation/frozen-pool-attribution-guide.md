# Frozen-pool A/B error attribution (offline)

**Status:** source-code capability, not proof of real-model improvement or S1 acceptance.
This document describes the read-only output added to the existing paired
\`ragos.retrieval.benchmark compare\` command.

## When to use

After the same authorized Dense query produced a frozen, versioned candidate
pool, compare that ranking with a rerank permutation of **the exact same**
chunk IDs. Both files must use \`page-ranking-trace-v3\` on **dev/holdout**
(not train), with an identical dataset, split, tenant, index, embedding
identity, case IDs, question text and gold labels.

The existing comparator rejects missing/added chunks or altered provenance;
this change only adds an aggregate interpretation of where the answer-bearing
(grade-3) page sits in that recorded pool.

\`\`\`bash
# Offline; no provider calls, no live data export.
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_page_benchmark.py
.venv/bin/python -m ragos.retrieval.benchmark compare \
  /secure/local/dense-dev-v3.jsonl \
  /secure/local/rerank-dev-v3.jsonl \
  --output /secure/local/paired-report-new.json
\`\`\`

The command uses a create-only private (0600) report file. Choose a **new**
output path; it must not already exist.

## How to interpret \`frozen_pool_error_attribution\`

- \`answer_page_missing_from_frozen_pool\`: no grade-3 gold page appears
  anywhere in the recorded candidates. Reranking that pool cannot recover it.
  Inspect parsing/indexing/query recall separately; **the report does not
  establish which upstream step failed**.
- \`answer_page_recalled_outside_top6\`: at least one grade-3 gold page
  is in the pool, but none is within the first six **unique pages**.
  This is a *ranking opportunity*, not a proof the ranker caused the miss.
- \`answer_page_in_top6\`: at least one grade-3 gold page is among the
  first six unique pages; this alone does not prove a correct generated answer.

\`baseline\`, \`candidate\` and \`transitions\` are **aggregate case counts**;
their totals each equal \`case_total\`. The report does not contain per-case
question text, source text, generated answers or raw IDs. Existing
\`hit_at_6_case_outcomes\` and NDCG/MRR deltas remain unchanged.

## Gate for claims

- Require human-reviewed labels and train/dev/holdout provenance before
  using these results for model selection.
- Do **not** call this measured real Recall@50 if the v3 trace was generated
  from mock ranks or a limited retrieval pool.
- Do not claim semantic citation accuracy, latency, actual tokens/cost,
  real Qwen quality, or production robustness from these outputs.
- Evaluate the full retrieval pipeline and real model weights separately;
  holdout is for frozen candidate promotion, not iterative tuning.
- Never send private trace content to an external observability platform
  without explicit tenant authorization and redaction review.
