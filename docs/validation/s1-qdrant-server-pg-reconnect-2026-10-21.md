# S1 Qdrant server + PostgreSQL published evidence contract (2026-10-21)

This is a **real HTTP Qdrant server integration test**, not a model-quality
benchmark, production durability sign-off or model-provider integration.
It extends the pre-existing Postgres + in-memory Qdrant PDF tests.

## CI setup

GitHub Actions starts disposable PostgreSQL 18 and Qdrant
`qdrant/qdrant:v1.19.1-unprivileged` services. It explicitly verifies the
Qdrant HTTP health endpoint before executing tests. Only
`RAGOS_TEST_QDRANT_URL=http://127.0.0.1:6333` authorizes the new Qdrant
test; non-local/remote target values fail rather than being used for writes.

## Contract tested

- Real 2-page synthetic PDF -> Poppler -> page parent/child index plan ->
  immutable local source artifacts -> actual Postgres source registration /
  preparation / fenced worker claim.
- Write Qdrant points **before publication**. `DenseRecall` receives
  candidate identities but published-only `SearchRepository` returns no
  usable evidence, so the staging chunks cannot be included in an answer.
- Complete the existing `IngestionService` dual-store reconciliation and
  CAS index publication, then close the first Qdrant HTTP client.
- Reconnect with a new Qdrant client to the **same running Qdrant server**.
  Re-read all projected points, verify their frozen index digest, and invoke
  actual `DenseRecall` with the PostgreSQL repository to hydrate the exact
  pages and document version. Qdrant has no plaintext `body` payload.
- Query as a different tenant and require zero results. Mutate a test vector
  payload's representation identity and verify the PG authoritative source
  check rejects it.

These tests create a unique per-run Qdrant collection prefix and delete that
collection in `finally`. The environment is disposable and receives **only
synthetic source text and mechanical hash-test embeddings**.

## Not demonstrated

A client reconnect is **not** a Qdrant server/container restart, node crash,
replica failover, snapshot restore or multi-node durable storage acceptance.
Neither the real Qwen embedding/Chat APIs nor the tenant ModelRouter is
activated by these tests. External Qdrant storage and automated disaster
recovery are not covered. Real PDFs, enterprise uploads, semantic citation
entailment and SFT/GRPO training remain separate work.

To reproduce with a **disposable local** PostgreSQL + Qdrant setup:

```bash
export RAGOS_TEST_PG_DSN='postgresql+psycopg://postgres:<test-password>@127.0.0.1:5432/<disposable-db>'
export RAGOS_TEST_QDRANT_URL='http://127.0.0.1:6333'
.venv/bin/pytest -q tests/test_qdrant_server_pg_reconnect.py
```

Do not point these test credentials or the Qdrant URL at a production
database. No DB schema migration, application provider credential, or
production deployment is changed.
