# S1: read-only chat run status and replay reconciliation (2026-10-21)

## Problem

PR #14 added a PostgreSQL-authoritative `ChatRequest` row to fence
concurrent duplicate chat POSTs. But after a network failure, clients had no
safe **read-only** way to distinguish a recently claimed run, a committed
answer ready for SSE replay, a committed answer whose JSONL log was lost, or
a failed/possibly billed operation. Retrying with a *new* idempotency key is
unsafe and may cause a second model call.

## API

`GET /chat/conversations/{conversation_id}/runs/{run_id}`

Requires verified chat-actor authentication and resolution of the tenant,
user and caller-owned conversation. The database query independently
requires matching tenant, user, conversation and run. Unknown and
foreign-actor runs return 404. There is no admin bypass, no raw idempotency
key, model request fingerprint, source text, prompt or token payload in
the response. It returns no-store and `X-Run-ID`.

Example shapes:

```json
{"run_id":"<uuid>","status":"claimed","replay_ready":false,"attention_required":false,"next_action":"poll_status","events_url":null}
```

```json
{"run_id":"<uuid>","status":"completed","replay_ready":true,"attention_required":false,"next_action":"replay_events","events_url":"/chat/conversations/<conversation_id>/runs/<run_id>/events"}
```

- **claimed (recent)**: `poll_status`, never generate or free a claim.
- **claimed (10+ minutes old)**: `operator_reconcile` and
  `attention_required=true`. The 10-minute age threshold is a warning,
  **NOT evidence that the worker is dead**; there is no heartbeat or
  distributed execution lease.
- **completed**: `replay_ready` becomes true only after the same bounded
  readback validation used by `GET .../events`: committed assistant
  message owned by this actor, first run_started event, matching run and
  conversation IDs, matching last run_finished and message ID. A torn,
  missing, oversized or invalid JSONL trace leaves SQL status completed,
  but reports `operator_reconcile` and no replay URL. **Do not rerun**
  the model just because the filesystem log was lost.
- **failed**: `operator_reconcile`; never automatically restart possibly
  billed provider calls or mutate the failure state.

The status endpoint is deliberately **non-mutating**: no claim release,
requeue, TTL-based lease stealing, provider call, SSE subscription, log
repair or unverified conversion from claimed to failed. This is a safe
diagnostic foundation for an explicit operator reconciliation workflow.

## Verification

- Route tests for completed+consistent SSE, completed+missing log,
  claimed recent/aged, failed, unknown and foreign-actor runs, and zero
  additional model calls / message writes on status reads.
- Real disposable PostgreSQL integration: actor-scoped read-only claim
  lookup (both before and after completion), cross-user denial, unknown
  run refusal.
- The existing completed-run SSE endpoint uses the **same** verification
  helper to avoid drift between a `replay_ready` report and replay rules.
- No schema migration; `0013_chat_request_claims` remains required.

Manual scoped regression:

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_chat_routes.py tests/test_chat_request_pg.py
```

Full PG/Qdrant CI is **manual-only** per `docs/CI_POLICY.md`; do not
auto-run on every push or assume old green checks validate the current HEAD.

## Remaining boundary

No heartbeat/lease, distributed running-stream resume, automated event
reconciliation, billing attribution or real Qwen end-to-end benchmark yet.
Client-visible `claimed` is **not** a guarantee that a worker is still
running. Once an operator proves the provider outcome, a separately
audited recovery state machine can be designed; never reset an ambiguous
claim just based on elapsed wall-clock time.

No user data, production service or DB schema is changed by this PR.
