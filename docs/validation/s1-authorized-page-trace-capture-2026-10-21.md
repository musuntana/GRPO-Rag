# S1: capture actual published page rankings into frozen JSONL (2026-10-21)

## Scope

`ragos.runtime.page_trace_capture.capture_page_ranking_trace` is an **explicit
operator/evaluation seam**, not an authenticated customer API or a Qwen
benchmark. It runs the existing `DenseRecall` against a tenant-scoped
PostgreSQL `SearchRepository`, which hydrates candidates returned by Qdrant.

For **one immutable PDF IndexPlan**, each reviewed answerable `EvalCase`
is checked against its tenant, KB, document ID, PDF page range, approved
label state, and `dev/holdout` split before any retrieval call. Only
`case.question` is supplied to `DenseRecall`; gold evidence is never
used to select, reorder or invent candidate pages.

The trace is serialized using the existing
`page-ranking-trace-v1` schema and validated by
`ragos.retrieval.offline_eval.evaluate_trace_bytes`.
The first line freezes dataset ID, tenant ID, index ID, embedding profile,
embedding version, retriever version and split. Each subsequent line
includes the case UUID, question, human-reviewed gold page identities and
the **raw hydrated rank order** as (document_id, page_no) pairs.

Repeated child chunks from the same PDF page are retained in the raw
trace, so the shared evaluator can deduplicate them and report the count
without converting a chunk-level search into an inflated page-level score.

Any returned candidate must provide actual nonempty PostgreSQL-backed
source text, a complete `EvidenceIdentity`, matching immutable
document-version/representation/index IDs and a validated positive page
number. Candidates from a foreign document, tenant, KB or index fail
closed. An empty list is a legal search result for an unanswerable retrieval
outcome; it produces zero hit/recall rather than a hallucinated page.

**Limit:** this v1 capture targets one PDF/document index per run. It
does not cover multi-document answer sets, cross-index version selection,
hybrid/RRF fusion or post-rerank Top-6. It accepts only reviewed answerable
questions with grade-3 gold; abstention is a separate metric.

## Tests with real servers

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_page_trace_capture.py
```

In CI, the positive end-to-end test uses a genuine synthetic two-page PDF
(Poppler), an indexed-task lease, **real PostgreSQL and Qdrant HTTP
services**, and the existing `LocalFrozenEmbedding` with fixed synthetic
OpenAI-compatible responses. It captures the same question against
staged vectors before index CAS publication (must return zero evidence)
and against the published index after a Qdrant client reconnect (must
return two distinct source pages, though four page-child points were
returned). The trace is then scored by the unchanged offline evaluator.

Other tests reject unapproved gold, split drift, foreign KB/tenant,
duplicate/invalid gold pages, and unversioned or fabricated responses
rather than granting a false evidence hit.

## Interpretation

This proves that a trusted **captured ranking really came through
DenseRecall -> published PostgreSQL evidence** with frozen source IDs.
It does **not** prove live Qwen has run. The embeddings remain deterministic
test fixtures, and no user PDFs, production tenant API keys, Reranker,
semantic citations or GRPO/LoRA weights are exercised in CI.

**Next:** a tenant-authorized operator can substitute a vetted real
embedding provider for the synthetic local fixture, explicitly record
its model digest, dimension, index and dataset IDs, and then compare
holdout traces under identical retrieval settings. Do not treat 2-case
synthetic scores as evidence for the 1,200-case historical results.

### Security/rollback

The trace **contains question strings and human gold labels**, so do not
publish real customer traces to GitHub or CI logs. Store them in an approved
private evaluation artifact store. There is no new upload route, DB
migration, provider admission or production configuration in this patch.
Rollback by reverting the isolated test/capture/docs PR.
