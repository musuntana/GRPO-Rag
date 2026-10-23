# S1: real local Embedding over published PostgreSQL/Qdrant PDF evidence

2026-10-23. Base `d6e74f2`; local branch `codex/s1-real-published-model-evidence`. This is an uncommitted acceptance-test addition, not a production feature or S1 exit. The sibling JSON freezes the exact test-source hash and measured results.

## Outcome

**PASS**: generated two-page PDF -> real Poppler -> frozen plan -> real tenant ModelRouter with database Provider/Profile/Binding resolution -> real local Qwen Embedding -> PostgreSQL publication/readback reconciliation -> independent Qdrant HTTP reader -> official dense channel -> PostgreSQL evidence hydration.

The operator run completed **3 passed in 3.43s**: one real integration case plus two safety regressions. It made **7 actual embedding HTTP calls**, used the pinned 1024-dimensional model, published four parent/child chunks for pages 1/2, and verified matching active index/source identities. Query text and page bodies are synthetic; no human-reviewed gold or ranking uplift is asserted. The reported 2.513s integration wall time is one run, not a latency benchmark.

Negative cases passed:
- An unbound tenant is refused before any model request.
- Another tenant with its own valid model binding may embed but cannot retrieve the original evidence.
- Wrong embedding settings fail before the model; wrong-version hydration returns no PostgreSQL text.
- The exact test-owned collection is removed after the run. Existing namespaces are not reused or deleted.

## Authorship and corrections

OpenCode authored the first test and its handoff. Codex's real run exposed an FK insertion-order error before model calls. Codex corrected Provider -> Profile -> Binding flush order and shared provider reuse, partial-failure resource cleanup, pre-request budgets, and portable create-only 0600 evidence output. The budget/no-overwrite regressions failed before those fixes and passed afterward. No product source, dependency, schema or shared fixture was changed.

OpenCode follow-up free models returned 429. A new free-model attempt was initially blocked by automatic approval for source disclosure; the user then explicitly authorized that destination, but it also returned 429. No further external retry is running. Final corrections and real verification were performed locally by Codex.

## Reproduction

Use an explicitly disposable, migrated local PostgreSQL database and dedicated local Qdrant HTTP service. Reuse the existing Alembic migrations; do not point these fixtures at a user database. The two temporary task containers used random loopback ports, 512 MiB/1 CPU limits and no host bind mounts. Exact image digests are in the JSON.

```bash
export RAGOS_RUN_REAL_LOCAL_MODELS=1
export RAGOS_TEST_PG_DSN='<disposable local postgresql+psycopg DSN>'
export RAGOS_TEST_QDRANT_URL='http://127.0.0.1:<disposable-port>'
export RAGOS_S1PE_LOCAL_BASE_URL=http://127.0.0.1:12434/engines/v1
export RAGOS_S1PE_LOCAL_CHAT_MODEL=docker.io/ai/qwen3:4b-instruct-2507-q4_K_M
export RAGOS_S1PE_LOCAL_EMBED_MODEL=docker.io/ragos/qwen3-embedding:0.6b-q8
export RAGOS_S1PE_LOCAL_EMBED_REVISION=79898cf0ff54015bf309900ccc25a340dd0ab011bbc78486e50abbcfb4ae908e
export RAGOS_S1PE_LOCAL_EMBED_DIM=1024
# Optional: a NEW evidence file path; existing files are never overwritten.
export RAGOS_S1PE_EVIDENCE_PATH=/private/tmp/ragos-published-evidence-new.json
.venv/bin/pytest -q tests/test_real_local_published_pdf_rag.py
```

Without the real-model gate the network acceptance case skips, while safety tests run: **2 passed, 1 skipped**, exit 0. Missing model identities fail; absent disposable-service configuration can skip through the existing fixtures. Always inspect skip counts, not only the pytest exit code. No stub is substituted for actual model responses. The chat model name is required by admission configuration but this test does not call Chat.

## Verification and boundaries

- Ruff: PASS. Existing PDF/frozen-embedding integration baseline on the temporary services: **13 passed**.
- Expanded local regression with real PG/Qdrant and real-model gate OFF: **1072 passed, 12 skipped, 1 failed**. The unchanged worker CPU/heartbeat test fails in isolation on macOS; `ProcessRunner.run` explicitly requires Linux. No assertion, permission or skip was loosened to hide this failure. This is not an all-green full regression or a new GitHub CI result.
- An intermediate rerun failed because the original temporary containers had disappeared; task-owned services were recreated and migrated before the successful run. No other database was used.
- The PG fixture connects as a disposable test superuser. Tenant/profile/filter negatives exercise application scoping, not production RLS-role enforcement or signed authentication.
- This invokes IngestionService directly, not the isolated worker process. Client reconnects are checked; database/vector server restart durability is not.
- Model revision is an operator identity assertion, not cryptographic weight attestation. No real enterprise gold, Chat from this published corpus, semantic citation validation, production budgeting, UI journey or training is claimed.

Rollback: remove this new test and its two evidence documents/baseline notice. No persistent schema or user data changed. Task-only containers/volumes are removed after logs and content-free evidence are saved. Original user working-tree modifications remain untouched.

Next product slice: define the minimal authorized upload -> index-status journey, retaining Linux worker isolation. Real corpus evaluation remains blocked on authorized documents and human page labels; do not replace these with model-generated gold.
