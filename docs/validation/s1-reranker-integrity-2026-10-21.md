# S1 Reranker evidence-integrity contract (2026-10-21)

## Failure fixed

The previous adapter accepted **subsets** or **duplicates** of candidate chunk IDs,
and checked only a subset of immutable metadata. A model adapter could change
`text`, `channel`, `layer`, page count or parent relationship and still have
its response forwarded towards Top-K evidence and citations. It also compared
against mutable originals *after* the external call.

## Contract

When a real rerank profile is configured, a model response must be an **exact
permutation** of the input head: no omissions, unknown IDs or duplicates.
Immutable `ScoredChunk` fields are snapshotted **before** the external call
and compared afterwards. Only rank order and a finite numerical score may
change; scored output is rebuilt from the trusted snapshot and exposes
`rerank_score` explicitly. Empty candidate lists never call the provider.

This is **adapter boundary hardening**, not an implementation of a real
Qwen reranker, a learned GRPO policy, or a semantic citation validator.

## Targeted validation

```sh
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_reranker_integrity.py tests/test_retrieval_pipeline.py
.venv/bin/ruff check packages/retrieval/src/ragos/retrieval/reranker.py tests/test_reranker_integrity.py
```

No model credentials, database migration, production data, Qdrant collection
or CI trigger change. Integration and genuine model ranking quality remain
unverified until executed. Follow `docs/CI_POLICY.md` before merging; manual
GitHub Actions Full verification applies if this change is combined with
identity or index-boundary changes.

## Compatibility and rollback

Existing rerank adapters intentionally returning only top-k candidates will now
fail closed. Instead, they must return a complete ordering of the *supplied*
head; `top_k_generate` selects Top-K **after** reranking. Rollback by reverting
the PR; no persistent schema or index rollback is required.
