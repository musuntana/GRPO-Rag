# RAG Workbench Phase 1: boundary and acceptance contract

Status: proposed; no implementation or real-model validation claimed.
Baseline: `docs/CURRENT_BASELINE.md` (2026-10-22).
This plan does not supersede existing security, tenant, indexing, or CI contracts.

## Repository decision
Keep RagOS runtime and GRPO-Rag offline experimentation in this repository for phase 1. A separate RAGOS repository has not been confirmed. Do not duplicate the model gateway, tenant identity, index versioning, or evidence schemas.

| Owner | Responsibilities | Exclusions |
| --- | --- | --- |
| RagOS runtime | tenant-scoped model gateway, ingestion/index manifest, PG/Qdrant, recall, authorized rerank, citations, trace capture | offline training, selection using holdout |
| GRPO-Rag offline | immutable trace consumption, train/dev/holdout split discipline, paired ranker experiments, negative mining, evaluation and optional later training | online model calls outside ModelRouter, mutation of published indexes |
| Integration adapters | optional RAGFlow/MinerU parsing, RagBuilder search, Langfuse/Ragas export | replacing authoritative PG state or copying secrets |

## Phase 1 slices
1. **Trace inventory**: map existing JSONL page traces and paired A/B evaluator to a documented schema. Preserve tenant scope, document/page/chunk identity, embedding profile/version, retrieval profile, index digest, model identity, and split provenance. Do not log private raw document text or credentials.
2. **Frozen baseline**: evaluate Dense recall and existing reranker on the *same* authorized, frozen candidate set. Record Recall@50, page Hit@6, MRR/NDCG, citation validity, latency, and estimated/observed cost separately. Missing measurements must be `not_measured`, never zero or success.
3. **Gold quality gate**: target at least 100 human-reviewed questions, stratified 50% common, 30% long-tail, 20% multi-hop. Enforce document/version provenance, explicit relevance labels, train/dev/holdout isolation, and no unknown-as-negative conversion. This is a target, not an existing dataset claim.
4. **External adapters (later)**: compare RAGFlow/MinerU parsing, RagBuilder tuning, Langfuse/Ragas experiment tracking against existing capabilities and licenses before choosing one adapter per responsibility. Default disabled; no network calls, secret forwarding, or production data export without approval.

## Acceptance / stop conditions
- Preserve all existing tenant/actor ACL, frozen embedding identity, PG active-index reconciliation, and Qdrant filters.
- Every paired A/B comparison uses identical query set, source versions, candidate set, and deterministic evaluation code; record profile and model digests.
- Test both authorized and cross-tenant/unknown-label negative cases. Run targeted Ruff/pytest locally first.
- Never report synthetic fixtures or mocked embeddings as real Qwen results. Record hardware, model weight/revision, dataset digest, run SHA, raw metrics and errors for real M1 tests.
- Stop before a production deployment, paid model call, training job, destructive reindex, or third-party data export.
- CI is manual only: Quick on final PR HEAD, Full only for storage/identity/model/cross-module changes; verify run SHA matches PR HEAD. Documentation-only PR may omit Full under `docs/CI_POLICY.md`.

## Immediate follow-up implementation PRs
- A: read-only trace schema validator + deterministic report with missing-metric markers.
- B: paired baseline CLI for existing frozen traces, no new model gateway.
- C: optional observability exporter behind explicit opt-in, with redaction and tenant authorization tests.

Rollback: revert these documentation changes; subsequent code PRs must specify their own rollback and migration plans.
