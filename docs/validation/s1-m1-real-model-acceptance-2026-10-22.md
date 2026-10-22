# S1 Mac M1: real Qwen acceptance runbook (2026-10-22)

**Status: documented only; no model run or quality claim in this PR.** Related: [Issue #22](https://github.com/musuntana/GRPO-Rag/issues/22). No synthetic test proves the real model, human-reviewed retrieval quality, production safety or GRPO uplift.

## 0. Environment and model provenance

On an operator-controlled Mac M1, use a clean checkout, Python 3.12 with uv, Poppler, local Docker Model Runner with OpenAI-compatible HTTP. Never use production credentials or unauthorized PDFs. Save the git SHA and `docker model list` with the *private* evidence bundle; the displayed revision is a claimed identity, not cryptographic weight attestation.

```bash
git status --short
git rev-parse HEAD
docker model list
command -v pdftotext
uv sync --frozen --no-editable --extra dev --python 3.12
```

**Stop** for dirty checkout, unavailable model, missing Poppler or failed locked install. Determine vector dimension from the actual embedding model, never from synthetic fixtures.

## 1. Actual Qwen embedding against in-memory Qdrant

```bash
export RAGOS_PROBE_BASE_URL='http://127.0.0.1:12434/engines/v1'
export RAGOS_PROBE_EMBED_MODEL='<real installed model ID>'
export RAGOS_PROBE_EMBED_REVISION='<observed lowercase hex revision, 12-64 chars>'
export RAGOS_PROBE_EMBED_DIM='<verified vector dimension>'
export RAGOS_PROBE_EMBED_PROFILE='qwen3_embedding'
.venv/bin/python scripts/check_frozen_embedding.py
```

**Pass:** genuine embedding HTTP request, finite nonzero vector of required dimension, nonzero returned candidates and projection digest. **Fail:** missing/wrong model or dimensions, invalid numbers, zero candidates, provider error. The script uses synthetic text and *in-memory* Qdrant: no durable index, human relevance or production ModelRouter is proven.

## 2. Actual Chat + Embedding + Poppler with generated synthetic PDF

```bash
export RAGOS_PROBE_CHAT_MODEL='<real installed chat model>'
.venv/bin/python scripts/check_local_pdf_rag.py --output /tmp/ragos-s1-pdf-smoke.json
```

**Pass:** successful exit and content-free report, checked against the exact checkout and model identifiers. **Fail:** missing report, parsing/model/citation failure. This checks a *synthetic* PDF, not the production upload API, real customer pages, signed tenant authorization or semantic citation entailment. Optionally set `RAGOS_PROBE_QDRANT_URL=http://127.0.0.1:6333` **only** for an explicitly disposable local Qdrant; the script uses a random scratch collection and cleanup.

## 3. Real published corpus and fair A/B: still blocked

Before claiming real quality, authorize a disposable PostgreSQL/Qdrant corpus and manually reviewed dev and *untouched* holdout page labels. Freeze index and embedding model/version/dimension, tenant/KB scope, rights, split hashes and page identities; rebuild old indexes rather than relabeling them. Use the existing tenant-authorized gateway with official Qwen3 Reranker `sentence-transformers.CrossEncoder` baseline and record exact model revision, tokenizer/prompt, precision/device, batch, rank depth, latency and cost. Local DMR *does not* expose a supported `/reranks` transport; never invent one.

Capture the same **real Dense candidate set** for the baseline and Reranker, then run the existing comparator over approved private traces:

```bash
.venv/bin/python -m ragos.retrieval.benchmark compare \
  /private/dev-dense.jsonl /private/dev-reranked.jsonl \
  --output /private/dev-comparison.json
```

Strict paired traces must share authorized cases, graded gold, dataset, published index, model version, page and chunk multiset. Cache cap: 20,000 chunks / 32 Mi source characters; partition into frozen batches if needed. Record Recall@50, Hit@6, MRR@10, NDCG@10, abstention on unanswerable cases, semantically verified citations, latency, token/compute budget and failure examples. An improved ranker does **not** repair failed initial recall. Select on dev before touching holdout. Training SFT/GRPO is a later, separate gate.

**Stop / reject:** leakage between splits, unreviewed labels, different candidate sets, unauthorized or cross-tenant evidence, stale SQL publication, model identity ambiguity, missing latency/cost or broken citation trace. Never reuse historical 63.5→86.2 as measurements of this checkout.

## Evidence and S1 boundary

For every operator run keep timestamp, exact git SHA, sanitized commands/exit codes, model name and revision assertion, observed dimension, index/dataset hashes, versioned metrics and sanitized errors. Store raw traces privately; never commit prompts, document text, credentials or tenant identifiers.

Passing stages 1–2 does **not** close Issue #22. S1 also needs real Stage 3 data and end-user **upload → index status → query → source-page opening** with appropriate authorization, error/abstention and budget behavior. This document supplies repeatable acceptance steps, not successful execution evidence.
