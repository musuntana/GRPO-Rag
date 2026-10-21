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
.venv/bin/python -m ragos.retrieval.local_probe
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

## Page parsing and immutable index identity upgrade

The new revision routes the synthetic per-page elements through
`parse_pdf_pages` and the actual `PageChunker(parent_unit="page")` before
calling Qdrant. The parser determines page numbers and parent-child spans;
the probe now validates that each child is an exact substring of both
the document-global representation and its page-local text. It no longer
invents `page_count=9` for a three-page fixture or uses an arbitrary
representation length of 1000.

Immutable identifiers derive from the source page list digest and frozen
parser/chunker settings. The vector index identity also includes the
embedding revision; two different embedding revisions must not reuse a point
or collection identity. A changed source page receives a new document
version and distinct chunk identifiers. This is a **local projection contract**,
not an authorized document-version record or published PostgreSQL manifest.

This test still starts with *already extracted* synthetic PDF page elements.
A real PDF byte parser and scanned-document OCR/vision, table layout recovery,
real tenant KB ingestion, durable PG publication and authenticated source
hydration remain separate features.

## Real PDF bytes -> page elements (Poppler text-only adapter)

`ragos.retrieval.parse.pdf_bytes.extract_pdf_bytes` now accepts genuine PDF
**bytes**, shells out without a shell to Poppler `pdftotext` in a disposable
private directory, and constructs the repository's `ParsedDocument` with
real 1-based page boundaries. `run_probe(pdf_bytes=...)` then reuses the
`PageChunker`, immutable representation/index identities, Qdrant writer,
scoped retrieval and structural citation verifier.

- Fixed `-layout -enc UTF-8 -eol unix` text-extraction profile; loss of
  table/diagram layout, image text, bounding boxes and metadata is a **known
  limitation**, not certified high-fidelity parsing.
- Poppler form-feed delimiters preserve page order. An unreadable, blank,
  scan-only or protected page rejects the complete operation instead of
  silently collapsing page numbers or inventing evidence.
- Default input cap 16 MiB, text-output cap 8 MiB, 200-page cap and 20-second
  process timeout. The parser writes only to a temporary private directory,
  disables stdout/stderr exposure and returns generic domain errors. These
  caps do not replace a real isolated worker's CPU/memory/disk limits.
- Source identity for PDF-backed probes derives from actual **PDF file bytes**
  (SHA-256), not merely extracted text. Representation identity also includes
  the extracted-text fingerprint so a changed extraction cannot silently
  reuse old evidence IDs. Embedding identity continues to select the index.
- A real two-page PDF byte stream is constructed directly inside the tests
  (no fixture file or PDF library); CI invokes the **real Poppler executable**
  and the installed page chunker, plus in-memory Qdrant integration. Tests
  also cover bad PDF magic, invalid binary, blank/scan-only pages, page/input/
  output caps and source-byte version changes. A missing local Poppler binary
  is a declared provider error; CI ensures one is installed.

Local setup (developer Mac):

```bash
brew install poppler
uv sync --frozen --extra dev --python 3.12
.venv/bin/pytest -q tests/test_pdf_byte_extraction.py
```

This does **not** expose an untrusted file-upload API or index user PDFs.
The CLI remains synthetic-only, intentionally, until tenant authorization,
persistent document ingest, egress/budget controls and parser process isolation
have been implemented. Do not send private enterprise PDF data to external
models from the dev probe.
