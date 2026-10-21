# S1 local ModelRouter admission (dev-only, not production provider onboarding)

Date: 2026-10-21. This closes one gap between the real local Qwen
`scripts/check_local_pdf_rag.py` smoke and the multi-tenant `ModelRouter`.
The existing local model HTTP adapter is reused; **no duplicate inference
client or encrypted API-key decryption system is introduced**.

## Explicit opt-in

The feature is **off by default**. It activates ONLY when the API server
is bound to loopback in `RAGOS_APP_ENV=dev`. Put these in a local, ignored
environment file, not the Git repository:

```sh
RAGOS_APP_ENV=dev
RAGOS_API_HOST=127.0.0.1
RAGOS_LOCAL_MODEL_ENABLED=true
RAGOS_LOCAL_MODEL_PROVIDER_ID=<UUID of registered local platform provider>
RAGOS_LOCAL_MODEL_BASE_URL=http://127.0.0.1:12434/engines/v1
RAGOS_LOCAL_MODEL_CHAT=<exact installed local Qwen chat model name>
RAGOS_LOCAL_MODEL_EMBED=<exact installed local Qwen embedding model name>
RAGOS_LOCAL_MODEL_EMBED_DIMENSION=<actual dimension, e.g. 1024>
```

These are not auto-discovered from the client request. The URL must be a
recognized local HTTP `/v1` endpoint; arbitrary IPs, credentials, paths
or remote HTTPS endpoints are rejected. Do not enable this option when
exposing the development API to a LAN or the internet.

The operator must use the *existing platform model-center endpoints*
to register an **active** provider with exactly:
- `provider_type=local_openai`
- `id=RAGOS_LOCAL_MODEL_PROVIDER_ID`
- `base_url=RAGOS_LOCAL_MODEL_BASE_URL`

Then use existing tenant model profile + tenant use-case binding to map
`chat` and `embed` separately to the **exact pinned model names** above.
The ModelRouter calls the existing `ProfileResolver` first, requires
matching tenant, binding/use_case, provider ID and model identity, and
re-checks active platform provider type and exact URL. It then bypasses
the encrypted `KeyPool` entirely, using the approved local endpoint
**without an API key**. All nonlocal providers remain unavailable.

## Safety and resource bounds

- Local-only Chat/Embedding; rerank/fallback/unpriced provider types refused.
- Chat input maximum 32 messages / 32,000 characters; default max_tokens
  256, maximum 512. Embedding up to 16 texts, each 1–8,000 characters.
- Returned Embedding length MUST equal the pinned dimension; NaN, Infinity
  and all-zero vectors are rejected. This is vector *shape* validation,
  NOT cryptographic attestation of Qwen weights.
- No arbitrary tenant-supplied model URL, no raw key intake, no ciphertext
  forwarded as bearer token, no automatic fallback to paid providers.
- The programmatic injection hook in tests is also forbidden outside
  loopback-bound development settings.
- Unit tests use a deterministic mock transport; no model is downloaded or
  billed by CI.

## Honest S1 boundary

This is a **development model gateway slice**, not completion of the
enterprise RAG user journey. The normal `RetrievalService.build_recall_channels`
still refuses real dense embeddings because the published PostgreSQL index
and real model's immutable identity are not yet bound end-to-end. Production
signed authentication/membership, quotas and cost ledger remain missing.
A local Chat and Embedding route working in isolation **does not validate
RAG citation quality or GRPO training**.

Verify the integration on the operator's own Mac via the existing
`scripts/check_local_pdf_rag.py` before enabling tenant bindings. Do
not deploy or expose development credentials. For production, replace
this opt-in path with a trusted credential resolver, budget admission,
per-tenant accounting and authenticated authorization reviews.

## Testing

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_local_model_router.py tests/test_model_gateway.py
```

Manual-only full CI may be run once for a ready-to-merge PR. The complete
suite uses mock model HTTP, disposable PostgreSQL and Qdrant; it cannot
claim real Qwen weights or model quality. No schema migration in this change.
