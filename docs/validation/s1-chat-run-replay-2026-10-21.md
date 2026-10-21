# S1 Chat SSE completed-run replay contract — 2026-10-21

## Defect addressed

Previously each POST `/chat/conversations/{conversation_id}/messages`
created a fresh `EventSink(EventLog(), run_id=uuid4())` and `ChatService.run_turn`
independently created a second run ID. A reconnect carrying
`Last-Event-ID` on POST therefore **recalled the model and wrote messages
again** rather than resuming the original log. The route docstring promised
replay that the API did not actually provide.

## Changes (development-scoped)

- A **new** POST creates one run UUID, binds `EventSink`, persisted user and
  assistant `MessageIndex.run_id` and `run_started/run_finished` to the same
  value. It publishes `X-Run-ID` for the client to retain. Passing an
  inconsistent run ID to the chat orchestrator fails before side effects.
- Any POST carrying `Last-Event-ID` is rejected as 422 **before** model
  invocation. The correct reconnect path is the authenticated GET below.
  This prevents a common replay-as-new-post bug; it is not a general
  idempotency key for arbitrary POST retries.
- `GET /chat/conversations/{conversation_id}/runs/{run_id}/events`
  returns **finite pages of an already completed run's SSE events**.
  `Last-Event-ID` skips acknowledged events; `limit` ranges 1–500.
  It makes no model or memory calls and never writes another message.
- Before reading events the endpoint resolves the verified actor's user,
  checks ownership of the parent conversation and requires a committed,
  tenant-scoped assistant message with the exact run ID. It then validates
  `run_started` and `run_finished` metadata against the requested
  conversation/run and the committed assistant message ID.
- Missing/torn/mismatched logs fail as not found instead of pretending
  completion. Readback is bounded (8 MiB, 2000 events) to limit accidental
  unbounded JSONL scans.

### Example client lifecycle

```text
POST /chat/conversations/<conversation-id>/messages
  200 text/event-stream, X-Run-ID: <run-id>
  id: 1
  event: run_started
  data: {"run_id":"<run-id>",...}
  ...
  event: run_finished

# After it has finished and the corresponding assistant message is committed:
GET /chat/conversations/<conversation-id>/runs/<run-id>/events
Last-Event-ID: 7
  -> only events with id > 7, no new generation
```

## Validation boundaries

Added route tests for run-ID lineage, replay without duplicated model calls,
correct SSE cursor, bounded result page, unknown/wrong run, unauthenticated
request, missing event files and explicit POST reconnect rejection. Chat
service tests cover mismatched run ID fail-closed before retrieval/message
writes.

Automated checks must be explicitly run under the repository's
manual-only GitHub Actions policy. The PR must **not** be merged based only on
source inspection or a green run from a different commit.

## Still missing

- **In-progress reconnect after worker/process restart** is NOT supported by
  this completed-only GET. A durable run state machine and persistent producer
  registration are needed; no process-global run registry is introduced.
- **Blind POST retries without Last-Event-ID** can still generate duplicate
  turns. A server-issued idempotency key, durable unique claim and state
  fencing should be added before production model billing.
- The existing `run_finished` SSE event is emitted before the request-level
  SQL commit; replay now refuses a missing committed assistant record, but
  the live streaming finality/commit ordering still needs a follow-up
  transaction redesign.
- No live provider, production user authentication, browser, or model-budget
  validation is claimed.

Rollback: revert the route/orchestrator/repository/test changes together.
No schema migration, provider credential, production run log, or stored
conversation was intentionally modified.
