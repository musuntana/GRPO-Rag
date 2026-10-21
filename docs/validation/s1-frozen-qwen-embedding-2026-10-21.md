# S1 local Qwen Embedding: frozen model and shared index/query identity

**Status:** opt-in local integration, not admitted multi-tenant Model Gateway.
This patch wires an OpenAI-compatible Embedding transport into the
**existing** immutable PDF IndexPlan, QdrantVectorIndexWriter and DenseRecall.

## Frozen model contract

A `FrozenEmbeddingSpec` requires:

- The exact installed model ID used in OpenAI-compatible `/embeddings`
  requests, for example a Docker Model Runner Qwen3 Embedding model;
- An operator-supplied immutable revision identifier (12–64 lowercase
  hexadecimal characters, copied from the exact installed model revision);
- A known vector dimension (1–8192) and explicit embedding profile.

The collection version includes the model-name fingerprint and supplied
revision. A different revision receives a **different Qdrant collection**;
`LocalFrozenEmbedding.require_plan()` refuses an IndexPlan whose profile or
version differs. An incorrect vector dimension, an all-zero embedding, NaN,
Infinity, or missing result returns a provider error, not a fake match.

**Important:** a 12-character model ID shown by `docker model list` is a
revision identifier, **not cryptographic proof** that weights are unchanged.
This module cannot verify installed weights or the provider's actual model
behind an alias; production must add trusted model attestation/admission.

## Automated validation

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_frozen_qwen_embedding_probe.py
```

The tests use an `OpenAICompatibleProvider` with a **fixture JSON transport**;
they assert actual request/response schema, error handling and the frozen
index identity. Real Poppler PDF bytes, PostgreSQL indexing/publishing and
Qdrant HTTP persistence/reconnect are exercised via CI's disposable servers.
**No actual Qwen model is downloaded or executed in CI.** These tests cannot
prove retrieval recall, citation entailment or embedding model quality.

## Real local Qwen smoke (synthetic source only)

On a Mac with Docker Model Runner TCP access enabled, check installed models:

```bash
docker model list
```

Then set the **actual** model ID, revision (hex), and embedding dimension
for that installed revision. Do not blindly reuse an example dimension or ID.

```bash
export RAGOS_PROBE_BASE_URL=http://localhost:12434/engines/v1
export RAGOS_PROBE_EMBED_MODEL='<model name from docker model list>'
export RAGOS_PROBE_EMBED_REVISION='<installed model revision ID in hex>'
export RAGOS_PROBE_EMBED_DIM='<actual embedding dimension>'
export RAGOS_PROBE_EMBED_PROFILE=qwen3_embedding
.venv/bin/python scripts/check_frozen_embedding.py
```

The script makes **real** /embeddings calls and writes only three **synthetic**
paragraphs to a temporary in-memory Qdrant client. It checks the same model
identity/dimension for writer and query and prints the readback digest and
candidate count. It does **not** use a customer PDF, production DB, durable
Qdrant volume, production ModelRouter or a real evaluation set.

Real cross-restart durability, audited token budgets, credentials/rotation,
tenant admission, signed application authentication, usage metering, and
production egress restrictions are still missing. **Never** register this
explicit local adapter as an enterprise provider or expose it to customers.

## Next milestones

1. Run the local smoke using the installed Qwen model and record model ID,
   digest/revision, output dimension, latency and exact probe output.
2. Introduce authenticated provider admission and resolved secrets with
   tenant-scoped budgets in the normal ModelRouter, without relying on the
   test-only opt-in.
3. Collect page-labeled evaluation cases, score real Recall@50 / Hit@6 /
   NDCG and citation consistency. Do not reuse historical figures under
   the new page-aware identity contract.
