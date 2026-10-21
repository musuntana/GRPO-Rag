# S1: durable Chat Idempotency-Key claims — 2026-10-21

## Why

The completed-only SSE replay and commit-before-finish fixes solved misleading
reconnect and finality behavior, but identical POST requests **without a
Last-Event-ID** could still execute model calls twice. An in-memory lock does not
work across Uvicorn workers or machine restarts.

This change introduces a **database-authoritative, fail-closed claim**, not an
unproven exactly-once distributed execution system.

## Contract

Every new `POST /chat/conversations/{conversation_id}/messages` requires a
`Idempotency-Key` HTTP header (12–128 URL-safe characters, generate a fresh
random value per *logical* message). Clients retry the **same** key and
**same** body after network uncertainty. The SQL unique key is
`(tenant_id, user_id, conversation_id, idempotency_key)`.

The request record stores a SHA-256 fingerprint of a canonical JSON request
including message, retriever profile and version; it does **not** store the
message text or model token data in the claim table. If a key is reused with a
different request body, return HTTP 409 without generation.

Before ANY model call, the server performs an atomic PostgreSQL
`INSERT ... ON CONFLICT DO NOTHING RETURNING id`, scoped to an authenticated
actor-owned conversation, and **commits the claim separately**.

- First claim owner: gets normal SSE and `X-Run-ID`, with that claim's UUID
  as the actual SSE and persisted message run_id.
- Same key, same request and `claimed` status: HTTP 202 JSON containing
  original run_id/status and replay URL. **No model rerun.** `claimed`
  means *claimed or possibly abandoned*, not necessarily a live worker.
- Same key, same request and `completed`: HTTP 200 JSON containing the
  original run_id/status and GET replay URL. **No additional model call.**
- Same key, same request and `failed`: HTTP 409 with original run ID/status.
  **No automatic replay of ambiguous external side effects**.

The assistant message, citations, memory writeback task and state
`completed` all commit in the **same SQL transaction**. Only after this
transaction succeeds may `run_finished` be appended to the SSE log.
Producer failure rolls back uncommitted writes and moves a still-`claimed`
record to terminal `failed` in a separate transaction. A previously
committed `completed` claim can never be downgraded to failed.

PostgreSQL enforces tenant row-level security and rejects modifications to
an immutable claim identity or any transition from terminal state. There is
no automatic claim expiry/release or "try again" on unknown in-flight state:
those need an operator-audited reconciliation before enabling model billing.

## Migration and validation

1. Migration `0013_chat_request_claims` creates the app.chat_request table,
   RLS, actor-scoped unique constraint, lifecycle check and immutable envelope
   trigger. Downgrade is refused while any claim records exist, preserving
   billing/idempotency evidence.
2. `REQUIRED_SCHEMA_REVISION` advances from `0012` to `0013`.
   Deploy database migration **before** deploying the new API release.
3. Unit/route tests: missing header rejects; matching replay never invokes
   retrieval or appends messages/tasks; mismatched body refuses; an active
   claimant remains non-reexecuting.
4. Actual PostgreSQL tests: actor isolation, fingerprint mismatch, terminal
   transitions, and concurrent unique-constraint contention with two sessions.

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/pytest -q tests/test_chat_routes.py tests/test_chat_request_pg.py
```

The PostgreSQL integration test requires `RAGOS_TEST_PG_DSN` pointing to
an **explicitly disposable local** database already migrated to 0013; without
it the PG tests skip and must not be described as persistence verified.

## Known boundaries

- This prevents duplicate model invocation **for the same client key only**.
  Two distinct keys intentionally represent two different messages.
  Clients must generate stable random keys per logical request and keep them
  until completion.
- A worker crash can strand a `claimed` record. Retry returns the old ID
  and must not restart uncertain billable work automatically. Durable leases,
  cancel/fencing, operator reconciliation and interrupted-run status endpoints
  remain future work.
- SSE event log remains filesystem JSONL, not a transactional distributed
  event store. SQL commit and event append can still diverge.
- It does not demonstrate real Qwen inference, semantic citation accuracy,
  actual billing correctness, multi-worker deployment or production signed
  authentication. Do not call this end-to-end exactly-once execution.

This migration is intentionally **non-destructive** and has no backfill of
older completed runs. Legacy replay of old message IDs remains available
under the prior actor/message authorization path.
