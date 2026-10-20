# S1 ephemeral page-RAG integration probe (2026-10-20)

Scope: **developer-only synthetic fixture**, not a deployed or authorized
enterprise ingestion/answer route. The probe reuses existing source identity,
Qdrant vector writer, readback, tenant filter, evidence metrics, generation
message builder and structural citation verifier. It does not copy document
text into vector payloads and does not use PostgreSQL.

## Path under test

```text
Synthetic contract pages 7, 8, 9 (one document version)
    -> model-gateway OpenAI-compatible embedding transport
    -> QdrantVectorIndexWriter (embedding model/version/dim frozen)
    -> Qdrant point readback + projection digest
    -> scoped Qdrant query (tenant_id + allowed KB)
    -> fixture-only source hydration by chunk_id, validate immutable lineage
    -> page-aware Hit@2 with page 7 frozen as answer-bearing gold
    -> grounded prompt -> model-gateway OpenAI-compatible chat transport
    -> citation extraction -> structural verification
```

No test creates a real document, publishes an index, writes PostgreSQL, or
uses another tenant's content. The example contract is invented by the tests.
`hit@2` from a single synthetic question is a smoke signal **not a quality
benchmark**. Structural `ok` means the cited page and text match supplied
evidence; it does **not** mean the model's claim is semantically true.

The local in-memory Qdrant client is disposed after the probe. A call from
the explicit CLI bypasses the database-backed ModelRouter; that router remains
fail-closed until entitlement, credential alias resolution, controlled egress,
metering and versioned model-binding admission are implemented.

## Offline CI validation

```bash
uv sync --frozen --extra dev --python 3.12
.venv/bin/pytest -q tests/test_local_page_rag_probe.py
```

CI uses deterministic fake embeddings and fake chat output, but real in-memory
Qdrant write/query/readback and the actual citation verifier. It explicitly
tests missing embedding revision, changed query vector dimension, bad marker,
and no plaintext source in vector payload.

## Live local Docker Model Runner check

Before running, enable Docker Model Runner's documented TCP endpoint on the
local Mac, check the exact model IDs using `docker model list`, and use the
**immutable pinned revision** of the embedding model/profile (not `latest`).
This probe uses synthetic text only.

```bash
export RAGOS_PROBE_BASE_URL=http://localhost:12434/engines/v1
export RAGOS_PROBE_CHAT_MODEL='<installed Chat model>'
export RAGOS_PROBE_EMBED_MODEL='<installed Embedding model>'
export RAGOS_PROBE_EMBED_REVISION='<frozen model revision or digest>'
.venv/bin/python scripts/probe_page_rag.py
```

The output contains the selected page numbers, page-level hit@2, Qdrant
readback projection digest, structural citation verdicts, and explicit
`semantic_verification=not_checked`. It does not print model prompts or
answers. The real model network probe and quality assessment are **not**
part of GitHub CI and must be explicitly executed on the machine holding
the models.

## Blockers for actual S1 user journey

- Real PDF/PPTX parsing into immutable versioned page-parent records
- Authenticated enterprise KB document ingest, durable PG index state and
  Qdrant reconciliation before publication
- Secret resolution, out-bound allowlisting, usage budget and tenant model binding
- Ingestion embedding model/version/dimension frozen through query-side recall
- Real CRAG judge, answer abstention, semantic citation verification and eval
- UI/user authorization, per-run idempotence/recovery and model metrics

Rollback: revert this stacked feature branch; it has no migrations or durable
stored data. PR #1 and #2 remain independent and must not be merged implicitly.
