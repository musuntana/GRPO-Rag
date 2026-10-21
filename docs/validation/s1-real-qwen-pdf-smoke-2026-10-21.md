# S1 real Qwen + genuine synthetic PDF smoke (operator-only)

This is the next practical test after frozen embedding identity, actual Poppler
PDF parsing, Qdrant readback and structural citation verification landed
separately. **It reuses those modules** rather than implementing a competing RAG
stack. The probe runs on an automatically generated three-page PDF with
synthetic contract facts. No enterprise documents are ever uploaded.

## Model and environment requirements

MacBook Pro M1 or Linux host with:

- Docker Model Runner (or another **locally bound OpenAI-compatible** endpoint)
  with a working Chat and Embedding model; enable its TCP endpoint explicitly;
- a known installed model ID, operator-recorded revision ID (12-64 lowercase
  hexadecimal chars), and actual embedding dimension from that exact model;
- Poppler `pdftotext` installed (macOS: `brew install poppler`);
- `uv sync --frozen --no-editable --extra dev --python 3.12`.

Check model IDs and current revision on **your own machine**; examples can
change across installations:

```bash
docker model list
export RAGOS_PROBE_BASE_URL=http://localhost:12434/engines/v1
export RAGOS_PROBE_CHAT_MODEL='ai/qwen3:4b-instruct-2507-q4_K_M'
export RAGOS_PROBE_EMBED_MODEL='ragos/qwen3-embedding:0.6b-q8'
export RAGOS_PROBE_EMBED_REVISION='<actual lowercase model revision hex ID>'
export RAGOS_PROBE_EMBED_DIM='<actual embedding vector length>'
.venv/bin/python scripts/check_local_pdf_rag.py \
  --output /tmp/ragos-local-real-pdf-smoke.json
cat /tmp/ragos-local-real-pdf-smoke.json
```

The model names above are **examples**; use the names from `docker model list`
and confirm the endpoint actually accepts them. A truncated model ID is not
cryptographic attestation of the weights. The script rejects missing values,
remote/model URLs and mismatched dimensions; a valid revision string cannot
prove the installed server is serving the specified weight bytes.

The default Qdrant backend is the real Qdrant **embedded client**. To also
exercise an actual local disposable Qdrant HTTP service, optionally set:

```bash
export RAGOS_PROBE_QDRANT_URL=http://127.0.0.1:6333
.venv/bin/python scripts/check_local_pdf_rag.py \
  --output /tmp/ragos-local-qdrant-http-smoke.json
```

This optional mode only allows loopback Qdrant endpoints, creates a unique
randomly named collection and deletes **only that scratch collection** even
when the probe fails. **Do not aim it at an existing production server or
shared dataset.** Both modes isolate synthetic tenant/KB IDs inside the probe;
they do not register enterprise tenancy, store customer documents or mutate
production PostgreSQL.

## Exactly what is exercised

1. Create deterministic real `%PDF-` bytes representing three **synthetic**
   contract pages (not a fake list of in-memory chunks).
2. Extract using actual Poppler `pdftotext -layout`, preserve physical
   1-based page identities, run existing `PageChunker` and build the
   immutable source/index identifiers.
3. Call the local OpenAI-compatible **Embedding HTTP API** for each page and
   the question; fail on wrong dimension, NaN/Inf or all-zero vectors.
   Bind the model name + operator-supplied revision + dimension into a
   frozen namespace.
4. Write to Qdrant, reconcile readback, query with a tenant/KB filter,
   validate source identity against the original immutable chunks.
5. Send only selected synthetic evidence to the local **Chat HTTP API** and
   verify generated citation *marker structure*. No raw model output or
   prompt text is written in the JSON report.
6. Emit JSON with `source_sha256`, embedding model/version/dimension, chat
   model, Qdrant mode, retrieved PDF physical pages, synthetic Hit@2,
   structural citation verdicts and source readback digest. Explicitly
   record `semantic_verification=not_checked` and unverified weight
   attestation.

**This is a single synthetic question, not a model quality benchmark.**
A result of `hit_at_2=1.0` is expected under strong models but must not be
reported as Recall@50 on enterprise data or GRPO/SFT uplift. A structurally
well-formed citation does not establish semantic entailment.

## CI and acceptance boundaries

Automated CI uses a deterministic `SyntheticProvider` fixture, genuine
synthetic PDF bytes and Poppler, plus in-memory Qdrant. With the disposable
`RAGOS_TEST_QDRANT_URL` set by full integration CI, a second test exercises
the **real Qdrant HTTP server**. CI **does not download or invoke Qwen
weights** or contact the operator's Mac endpoint. The real model smoke is a
separate **manual acceptance action** and must capture its output report
locally; never commit private data or credentials.

```bash
.venv/bin/pytest -q tests/test_local_real_pdf_qwen_probe.py
```

Next real product milestone: bring the approved local model transport behind
a securely admitted tenant ModelRouter, then use a real immutable
PostgreSQL-published PDF index, authenticated question requests, and labeled
holdout page-level evaluation. Production signed auth, budgets and
model-weight attestation remain unavailable in this CLI.
