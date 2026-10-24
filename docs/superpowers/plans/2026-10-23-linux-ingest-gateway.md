# Linux ingest worker real-gateway implementation plan

> Implementer: OpenCode; integrator/reviewer: Codex. User approved slice 1 on 2026-10-23. Follow the existing test-driven execution and verification workflow. No commit/push unless separately authorized.

**Goal:** Allow an explicitly enabled development Linux ingest job to write pinned real local embeddings through the real tenant ModelRouter; preserve the existing test/hash path and refuse production.

**Architecture:** Runtime owns a small worker embedding adapter using the existing FrozenEmbeddingSpec as the only identity object. ModelRouter gains an optional explicit Settings dependency so child jobs never need ambient .env configuration. Reuse Task claims, fencing, IngestionService, QdrantVectorIndexWriter per-text embedding and ProcessRunner; no batch interface, API, migration or new persisted states.

**Tech:** existing Python3.12/SQLModel/PostgreSQL/Qdrant/model gateway/pytest/ruff.
**Spec:** ../specs/2026-10-23-upload-index-status-design.md sections6/10.2, narrowed as below.

## Constraints and ownership

- OpenCode owns the exact implementation files below, one writer. Codex owns local test services/evidence and reviews. Original /Users/musun/GRPO-Rag is read-only.
- Dev execution requires app_env=dev, ingest_local_model_enabled=true, local_model_enabled=true, loopback api_host, valid existing LocalModelAdmission, Linux and a frozen server spec matching configured embedding_profile/version. Production still refuses before DB/vector/model work. Test/hash guards remain unchanged.
- New Settings field: ingest_local_model_enabled: bool=False. No secrets or identity from task payload; ingest payload remains exactly manifest_id.
- Reuse FrozenEmbeddingSpec with profile=settings.embedding_profile, model/revision/dimension from trusted local settings. No duplicated mutable profile/version fields; no metadata in ModelProfile.parameters.
- ModelRouter(session, *, allow_test_stub=False, local_model=None, settings: Settings|None=None) is additive. Existing callers keep current behavior; worker passes settings explicitly. All actual embedding calls still use router.embed, real profile/binding/provider admission and scoped DB sessions.
- Existing per-text EmbeddingFn only. No vector/storage/task-repository changes, batching, upload/status routes, parsing, UI, auth weakening, training, dependency changes, or production activation.
- The trusted job runs in a separate bounded Linux process group. This is lifecycle isolation, not a filesystem/network sandbox. Untrusted PDF parsing remains out of scope and production remains disabled.

## Allowed files

- packages/core/src/ragos/core/config.py (one disabled-by-default worker flag)
- packages/model-gateway/src/ragos/model_gateway/router.py (optional explicit Settings injection only)
- packages/runtime/src/ragos/runtime/worker_embedding.py (new: worker settings validation, pinned adapter)
- packages/runtime/src/ragos/runtime/ingest_job.py (mode selection, real gateway writer, cleanup)
- apps/ingest-worker/src/ragos_ingest_worker/main.py (dev registration and explicit child environment)
- tests/test_ingest_gateway.py (new) and tests/test_ingest_handler.py (existing guards/child env)
- tests/test_local_model_router.py (only explicit Settings injection regression if needed)

## Task 1: pinned worker adapter and explicit settings

Interfaces: validate_worker_embedding_settings(settings: Settings) validates the selected hash/dev mode before resources; WorkerEmbedding(settings, session_factory, tenant_ctx) stores spec once, preflight() authorizes the real embed binding with no model request, embed_one(text: str)->list[float] calls ModelRouter.embed in a fresh tenant-bound session and validates exact count/dimension/finite/nonzero vectors. Names may be refined only coherently across callers/tests.

- [x] Write failing tests for production/default-disabled/non-Linux/missing admission/wrong frozen version refusal before DB/network, correct trusted profile/version, and explicit Settings without ambient getter access.
- [x] Observe expected failures, implement the minimal adapter and additive router/config changes.
- [x] Cover invalid vector count/dimension/bool/NaN/zero and fresh scoped sessions. Model HTTP fixtures do not count as real-model acceptance.

## Task 2: existing isolated ingest path

- [x] Add failing tests preserving old hash behavior and rejecting untrusted payload/tenant/task/attempt references.
- [x] Keep existing task claim/manifest/lease guards authoritative. Resolve real model binding before constructing vector side effects; no long DB write locks over HTTP.
- [x] Use the adapter embedding_fn for real dev writer, verify frozen spec matches existing manifest via runtime checks; keep explicit test/hash writer unchanged.
- [x] Build only a server-owned env allowlist (DB, storage, vector, explicit dev/model/frozen flags and loopback API host). Do not copy os.environ, inherited secrets or task-provided URLs. Close vector clients in finally.
- [x] Run targeted Ruff/pytest against a freshly rebuilt non-editable package. No PYTHONPATH workaround.

## Task 3: independent acceptance and review (Codex)

- [x] Review auth-before-provider, stale claim/cancel fencing, no production activation, identity consistency, child env and resource cleanup.
- [x] Linux ProcessRunner + real disposable PG/Qdrant + local model acceptance where runtime/network available; record exact source hashes and identities. Never patch sys.platform for real acceptance.
- [x] Verify hash regressions and tenant/version negatives. Document actual model vs fixture boundaries, skip reasons and Mac platform limits.
- [x] Update baseline/evidence only after actual results. User approval of this plan does not imply S1 exit, production use, deployment or new model spend.

## Review focus

Wrong/changed tenant model binding; wrong manifest model space; stale task attempt/cancel; ambient configuration/secrets in child; vector/model failure leaking resources. Each must be covered by task-owned tests and final review. Default flags and production refusal remain mandatory.
