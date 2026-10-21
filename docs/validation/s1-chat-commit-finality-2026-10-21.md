# Chat completion only after SQL commit — 2026-10-21

## Problem fixed

Before this change, `ChatService.run_turn()` wrote `run_finished` to the SSE
JSONL log **before** the API route committed the SQL transaction containing
the user message, assistant message, citations and memory-writeback task.
A commit failure could therefore leave a terminal SSE event for an answer
that did not exist in PostgreSQL. An SSE client might treat that completion
as durable even though authenticated replay would later refuse it.

## Change

`ChatDependencies.persist_turn` is an **explicit required** transactional
persistence callback. Production composition injects `session.commit`.
The turn executes this callback *after* preparing the user and assistant
message rows and scheduling memory writeback, **before** emitting either
`memory_writeback_scheduled` or `run_finished`.

If SQL commit raises, no terminal SSE event is published. The API producer
rolls back the transaction and closes the sink so consumers do not wait
indefinitely. The event log remains an auditable **partial** run (e.g. token
deltas may have been delivered), not a successful replayable run. The
completed-run GET still requires a committed assistant message and matching
run metadata to replay the event stream.

## Test contract

- A fake persistence callback inspects message and task records and verifies
  no terminal event has been emitted before commit.
- A deliberately failing persistence callback verifies `run_finished`
  and `memory_writeback_scheduled` remain absent.
- Existing chat route, event-log and completed-run replay tests remain enabled.
- Database and API CI must be manually invoked per `docs/CI_POLICY.md`;
  a green run on an older head must not be presented as validation of this PR.

## Remaining gaps

This is **not** a durable exactly-once request protocol. Blind POST retries
without a cursor can still regenerate and rebill. Implement a per-actor,
per-conversation idempotency-keyed persistent run claim with a unique DB
constraint, plus an explicit state machine and fencing for stale producers.
For in-progress SSE reconnect, use a process-independent producer/lease
registry; do not pretend completed-only GET attaches to a live generator.

The event log itself still resides on a configured filesystem rather than
a distributed event store. SQL commit can succeed while a later filesystem
append of `run_finished` fails; recovery and reconciliation of committed
but unacknowledged runs require a follow-up design. This PR is a
**commit-before-success boundary**, not atomic commit across SQL and JSONL.

No migration, credential, model billing, or production data is changed.
