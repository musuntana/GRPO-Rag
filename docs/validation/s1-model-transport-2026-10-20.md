# S1 model transport: explicit OpenAI-compatible probe

**State (2026-10-20):** Transport adapter plus tests, **not** a connected tenant
Model Gateway and **not** an accepted end-to-end RAG baseline.

## Contract

- `OpenAICompatibleProvider` sends synchronous, bounded, non-streaming
  `POST {base_url}/chat/completions` and `POST {base_url}/embeddings`.
  Embedding outputs are reordered by returned `index`; duplicates, missing
  vectors, inconsistent dimensions, NaN and Infinity are rejected.
- The optional `POST {base_url}/reranks` contract expects every input
  document's unique `index` and finite `relevance_score`. Missing or
  unsupported reranking returns a model provider error, not fabricated scores.
- HTTP requires an explicit /v1 URL, a bounded timeout, disabled redirects and
  proxy inheritance, and a response-size cap. Plain HTTP is accepted only for
  explicit local hostnames. Remote providers require HTTPS.
- Keys may only be supplied to the local explicit probe via an environment
  variable. The class suppresses upstream exception bodies and never logs
  model prompts or API key values.

This adapter has **not** been enabled by `KeyPoolManager` or `ModelRouter`.
The existing platform key pool contains `api_key_encrypted`, but no verified
decryption/resolution path. Treating that value as a bearer token, enabling
unrestricted egress, or using the adapter to bypass tenant budgets would violate
the existing security contract. The provider factory must remain fail-closed.

## Reproducible local checks

Install the repo's documented frozen Python 3.12 environment. To verify
the adapter without any paid inference:

```bash
.venv/bin/pytest -q tests/test_openai_compatible_provider.py
```

To run a *real, explicit* smoke request from a Mac host with Docker Model
Runner TCP access enabled, inspect `docker model list` to get the **exact**
installed model names, then configure:

```bash
export RAGOS_PROBE_BASE_URL=http://localhost:12434/engines/v1
export RAGOS_PROBE_CHAT_MODEL='<chat name from docker model list>'
export RAGOS_PROBE_EMBED_MODEL='<embedding name from docker model list>'
.venv/bin/python scripts/check_model_api.py
```

The smoke script uses a fixed greeting plus a synthetic page example and
reports only response character count and embedding dimension. It does not
ingest a real PDF, modify a tenant model binding, or save vectors in Qdrant.

For a remote OpenAI-compatible provider, set an HTTPS /v1 base and
`RAGOS_PROBE_API_KEY` in the invoking shell (never commit the key).
Rerank is optional: set `RAGOS_PROBE_RERANK_MODEL` **only when that exact base
URL exposes `/reranks`**; Docker Model Runner docs list chat and embeddings
but do not claim a /reranks route. Some Qwen-compatible services implement
/reranks as a separate extension. A 404 means **not supported**, not a zero
relevance score.

- Docker Model Runner: https://docs.docker.com/ai/model-runner/api-reference/
- Qwen-compatible /reranks contract: https://docs.qwencloud.com/api-reference/rerank/openai-rerank

## Verified vs remaining

- **CI can prove:** request construction, parsing, out-of-order embedding
  index handling, numeric validation, missing-score failure, local fake-server
  POST wire format, and existing repository gates.
- **CI cannot prove:** installed model compatibility, real latency/cost,
  real answer quality, semantic citation accuracy, or any training gains.
- **Still required:** authorized provider admission and secret alias resolution;
  credential rotation, outbound network policy and tenant budgets; real
  provider health and retry policy; exact embedding model/version/dimension
  freeze in Qdrant; actual PDF ingestion + SearchRepository hydration and
  citation verification; frozen eval bundle.
- **Rollback:** revert the stacked provider PR branch; the persistent schema,
  index pointer and tenant/provider binding are unchanged.
