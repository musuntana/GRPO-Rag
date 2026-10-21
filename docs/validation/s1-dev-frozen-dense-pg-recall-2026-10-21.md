# S1 development DenseRecall bridge — 2026-10-21

## Problem

The loopback-only dev ModelRouter in PR #18 can call local Qwen Chat/
Embedding, but `build_recall_channels` still only admitted the deterministic
hash-test Dense route. Querying the vector store without checking the
PostgreSQL manifest embedding identity could silently hydrate an active
index from a different embedding space. A same-dimension vector is NOT
sufficient proof that model versions are interchangeable.

## What is added

- Operator-provided `RAGOS_LOCAL_MODEL_EMBED_REVISION` is a 12–64
  lowercase-hex recorded model revision. This is a **claim**, not
  cryptographic attestation that Docker Model Runner serves those weights.
- The dev-only Dense build path requires:
  - `RAGOS_APP_ENV=dev`, `RAGOS_LOCAL_MODEL_ENABLED=true` and
    `RAGOS_API_HOST=127.0.0.1` (loopback).
  - Local Qdrant HTTP endpoint, an admitted `ModelRouter` local provider,
    exact configured Embedding model name and expected dimension.
  - Explicit `RAGOS_EMBEDDING_PROFILE` and
    `RAGOS_EMBEDDING_VERSION` matching the same
    `FrozenEmbeddingSpec(model, model_digest, dimension, profile)`.
  - On query, `GatewayEmbedder` (tenant-bound ModelRouter) generates the
    query vector; `DenseRecall` searches the frozen stable collection.
  - PostgreSQL hydration checks active `ChunkManifest` publication as
    before **and now also filters the manifest's embedding_profile and
    embedding_version** against the requested frozen space. Point payload
    text remains untrusted; all evidence text originates in PostgreSQL.
- `LocalFrozenEmbedding.dense()` also passes its frozen identity to SQL
  hydration, so the probe and official dev bridge use the same guarantee.
- Default/test hash-vector path remains unchanged. Production and unadmitted
  model routes remain blocked; no migrations or destructive writes.

## Example local configuration

Use the exact output from the operator's own frozen model probe; do not
copy synthetic test IDs into a real data index:

```sh
RAGOS_APP_ENV=dev
RAGOS_API_HOST=127.0.0.1
RAGOS_LOCAL_MODEL_ENABLED=true
RAGOS_LOCAL_MODEL_PROVIDER_ID=<operator-owned platform provider UUID>
RAGOS_LOCAL_MODEL_BASE_URL=http://127.0.0.1:12434/engines/v1
RAGOS_LOCAL_MODEL_CHAT=<actual local chat name>
RAGOS_LOCAL_MODEL_EMBED=<actual local embedding name>
RAGOS_LOCAL_MODEL_EMBED_DIMENSION=<observed vector length>
RAGOS_LOCAL_MODEL_EMBED_REVISION=<operator-recorded immutable hex revision>
RAGOS_QDRANT_URL=http://127.0.0.1:6333
RAGOS_EMBEDDING_PROFILE=qwen3_embedding
RAGOS_EMBEDDING_VERSION=<computed FrozenEmbeddingSpec.version>
```

Index publication must use that exact frozen embedding identity and
`SearchRepository` will only deliver **reconciled, indexed, active**
source rows whose manifest identity matches the query. A previously
published hash-test index cannot be repurposed to a real model by renaming
the collection or changing a setting. Shadow rebuild and readback against
the real model are required; never directly mutate production vectors.

## Tests

A new disposable database and Qdrant HTTP integration test builds a real
synthetic PDF through Poppler, prepares/publishes the frozen index with a
deterministic embedding transport, then enters the official
`build_recall_channels` entrypoint to query the active published evidence.
The test checks the query was routed through a tenant-scoped gateway and
that mismatched PostgreSQL embedding versions return zero trusted chunks.
Other tests reject wrong configured version or remote Qdrant.

Run locally on a disposable database (never production):

```sh
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_frozen_qwen_embedding_probe.py
```

GitHub CI uses synthetic model responses only. The verification is a
structural/end-to-end **transport** contract, not actual Qwen model quality
or a production end-user experience.

## Remaining blockers

This does NOT itself implement the general production PDF ingestion API,
a trusted model-weight attestation, cost ledger, private tenant quotas,
production JWT/member checks, OCR/multimodal layout or long-running worker
recovery. Real Chat+Embedding result quality, gold-page Hit@K and citation
entailment still require a labeled holdout dataset and a run on the
operator's real installed models. The current model identity is recorded
by configuration; never describe it as verified by server-side proof.
