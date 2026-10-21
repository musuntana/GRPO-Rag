# S1 PDF immutable plan -> PostgreSQL/Qdrant validation (2026-10-21)

## Scope

This change connects the already validated real PDF-byte text parser
(Poppler) to the **existing frozen representation and transactional index
publication protocol**. It does not add a new repository or a new storage
backend.

Two pure builders under `ragos.runtime.pdf_index`:

1. `freeze_pdf_representation` creates a `FrozenRepresentation` whose
   page spans reference the precise document-global text, preserving 1-based
   PDF page numbers and never omitting a blank/scan-only page.
2. `build_pdf_index_plan` constructs an `IndexPlan` from the repository's
   `PageChunker`, including every page parent and text child. Each source
   span must round-trip byte-for-character against the original frozen
   representation and its page boundaries. A synthetic repeated table-header
   child or any malformed offset fails closed rather than fabricating a
   citation into original PDF text.

These objects are **inputs** to the existing `ArtifactStore`,
`DocumentVersionRepository`, `DocumentRepresentationRepository`,
`IndexPreparationService` and `IngestionService`. This avoids bypassing
the production guard for claimed ingest tasks, tenant-scoped DB transactions,
two-store reconciliation, CAS publication and idempotent replay.

## CI test

`tests/test_pdf_pg_index_integration.py` makes a valid synthetic two-page
PDF **byte stream**, extracts text with real Poppler, records source and
representation in immutable artifacts/PG records, prepares and claims a real
`ingest_document` task, runs the genuine PG + in-memory Qdrant index writer,
checks both reconciliation digests and the published active index, and reads
back document/page text from the authorized SQL search repository. Tampering
with the source artifact before execution must block publication.

```bash
.venv/bin/pytest -q tests/test_pdf_pg_index_integration.py
```

The disposable PostgreSQL integration fixture requires a separately supplied
`RAGOS_TEST_PG_DSN` pointing at a migrated local database; without it the
tests skip instead of claiming persistence. GitHub CI provisions the local
PostgreSQL container. Hash vectors are explicitly test-only; no real
Embedding/Reranker, tenant Model Gateway, scanned PDF OCR, image/table
fidelity, semantic citations or model training is claimed.

## Still not completed

No external user upload route or deployable ingest worker with a real provider
has been activated. The parser subprocess is not yet protected by a production
CPU/memory/disk sandbox. The PG-backed path is a **trusted synthetic local
integration test**; real KB upload authorization and source hydration on the
query-side dense route, provider admission, cost attribution, and end-to-end
quality evaluation remain separate gates.

Rollback: revert this isolated stacked PR. No migration, tenant binding or
production index was changed.
