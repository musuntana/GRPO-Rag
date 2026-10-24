# 2026-10-23 — Minimal PDF Upload → Durable Index Status (Proposed Design)

**Status:** proposed design only. No product code is implemented by this document.
**Revision:** 2026-10-23 review revisions applied (embedding provenance, route
namespace, slice 1 scope, request digest, cancellation derivation, evidence
provenance). The complete journey remains **Proposed**. The user approved only the Linux ingest gateway slice on 2026-10-23; upload/status/UI slices require separate implementation review.
**Baseline:** public `main` at `d6e74f2d10d94f759a5436e1528f8106b798582b`, branch
`codex/s1-upload-journey-contract`.
**Scope:** the minimum authorized PDF upload → durable index-status journey, reusing
existing modules. No training, multimodal/OCR, workflow framework, generic plugin
system, full admin UI, production deployment, or customer-data acceptance.

This document is the contract to review before a later implementation PR. It
deliberately describes *missing seams*, not working behavior.

---

## 0. Decision summary

1. **Reuse the existing records.** The journey uses `DocumentRecord`,
   `DocumentVersion`, `DocumentRepresentation`, `ChunkManifest`, and `Task`.
   No parallel document, task, permission, or queue model is introduced.
2. **Two asynchronous phases.** A new `prepare_document` task parses the PDF,
   freezes the representation, builds the index plan, and enqueues the existing
   `ingest_document` task. Indexing remains `IngestionService.execute`.
3. **Status is a projection.** `GET .../index-status` reads persisted rows and
   returns a derived phase. It never starts work, never calls a model, and never
   mutates a task or index.
4. **Upload idempotency uses `Task`.** The upload route writes the immutable
   source artifact, then atomically creates the document/version and enqueues a
   `prepare_document` task under a server-derived idempotency key.
5. **The real-gateway boundary is defined first and implemented first.** The
   worker authorizes the tenant's real `embed` binding and freezes the vector
   identity from trusted, server-side local model pins plus the existing
   `FrozenEmbeddingSpec`; it never reads identity from request data, task
   payload, or `ModelProfile.parameters`. The current `ingest_job.py` test/hash
   gate is the confirmed blocker, and the first implementation PR is only this
   worker slice.
6. **Production remains disabled.** Signed member/resource auth, secrets/budget,
   and Linux runner isolation are not implemented. A local dev journey is
   explicit opt-in, loopback-only, and truthfully labeled.
7. **Routes are canonical plural.** The canonical namespace is
   `/tenant/knowledge-bases`; the current `/tenant/kb` prefix is migrated in one
   batch with all callers and tests, with no permanent alias. Baseline facts
   below describe the pre-migration tree and are deliberately retained.
8. **UI comes last.** A single dev-only server-served HTML page is proposed for
   the final slice; it consumes the real API and does not create a second
   authorization path.

---

## 1. Problem and confirmed blocker

The public code already contains the immutable source/index protocol and the
task/lease machinery, but it does not expose an authorized PDF upload journey.
The route paths in this section are the **current `main` facts** under the
legacy `/tenant/kb` prefix; §4.0 migrates that prefix in one batch.

- `POST /tenant/kb/{kb_id}/documents` creates metadata only. There is no route
  that accepts PDF bytes, stores an immutable source, and enqueues preparation.
- `POST /tenant/kb/{kb_id}/documents/{document_id}/ingest` is deliberately
  unavailable and returns `ValidationError` (`routes/kb.py:198-213`).
- `IndexPreparationService.prepare(...)` exists but is called only from tests
  and operator scripts; no worker handler or API creates a preparation task.
- `ingest_job.py` admits only `app_env == "test"` with
  `task_test_embedding=True` and the frozen `hash-test` / `sha256-hash-v1`
  identity (`packages/runtime/src/ragos/runtime/ingest_job.py:19-26`). The
  ingest worker performs the same check
  (`apps/ingest-worker/src/ragos_ingest_worker/main.py:38-44`).
- `ProcessRunner` refuses non-Linux (`process_runner.py:86-87`) and is
  explicitly lifecycle isolation, not a security sandbox
  (`process_runner.py:24-31`). A macOS service-level smoke cannot prove worker
  execution.
- The only authentication is an unsigned `dev.` bearer token
  (`packages/auth/src/ragos/auth/jwt.py:28-49`). Production startup already
  refuses (`apps/api-server/src/ragos_api/main.py:32-36`), but the upload
  journey must not silently turn that into a production capability.

**Confirmed blocker to respect throughout:** no upload capability (let alone a
UI) can be called “working” while the worker only accepts test/hash embeddings.
The real gateway boundary in §6 is designed and implemented **first**, as slice
1, before upload/status/UI slices depend on it. The service-level acceptance does
not prove worker execution.

---

## 2. Current public baseline (read-only facts)

| Area | What exists | What is missing for this journey |
|---|---|---|
| Document identity | `DocumentRecord` (`models/app.py:56-79`), `DocumentVersion` (`591-615`), `DocumentRepresentation` (`618-658`) | No upload route, no route to register a version from bytes |
| Index identity | `ChunkManifest` (`229-285`), immutable plan/preparation (`index_preparation.py`), fenced indexing (`ingest.py`) | No preparation worker, no status projection, no retry/cancel product semantics |
| Tasks | `Task` states and leases (`models/app.py:317-345`), retry/backoff/cancel (`repositories/tasks.py`), process dispatch (`runtime/tasks.py`) | No `prepare_document` kind, no typed domain error codes for the worker |
| Artifacts | `ArtifactStore` with tenant-scoped keys/digests (`storage/artifacts.py`) | No API upload path; no source-page reader |
| PDF parsing | `extract_pdf_bytes` with Poppler bounds (`parse/pdf_bytes.py`) | Only called from tests/operator CLI; not wired to a worker |
| Embeddings | `ModelRouter.embed` (`model_gateway/router.py:139-161`), local admission (`local_admission.py`), local probe (`local_embedding_probe.py`) | No tenant-bound, frozen ingestion embedding route; `ingest_job` is test/hash only |
| Auth | Dev token + role check (`auth/deps.py:30-31`), tenant resolution (`tenant/resolver.py`) | Signed auth, membership, KB ACL |
| UI | No `apps/web`; no server-rendered surface | A dev-only page after the backend contract |

The existing `DocumentRecord.ingest_status` is `pending` by default and set to
`indexed` only by `DocumentRepository.publish_index`
(`repositories/kb.py:148-185`). Migration `0011` can set `failed` for legacy
unreconciled rows. There is no `cancelled` transition today.

---

## 3. Domain model reuse (no parallel model)

### 3.1 Records and owners

| Record | Owner of business meaning | Role in this journey |
|---|---|---|
| `DocumentRecord` | KB ingestion | Stable document identity, coarse `ingest_status`, `active_index_id` |
| `DocumentVersion` | KB ingestion | Immutable source bytes identity: `source_digest`, `source_ref`, `media_type` |
| `DocumentRepresentation` | KB ingestion | Frozen parsed representation: text, page spans, parser/render profile |
| `ChunkManifest` | KB ingestion | Frozen index plan, `indexing_state`, readback digests, publication facts |
| `Task` | Runtime | Durable work envelope, lease/fencing, attempts, cancel, terminal state |
| `ArtifactStore` | Storage adapter | Scoped immutable bytes for source, representation, and plan |

There is **no new table, no new queue, no new permission model, and no second
status machine**. This design adds **no new persisted state value**: it reads
and writes only states that already exist, and derives presentation phases from
`Task` and `ChunkManifest` rows.

### 3.2 Trusted identity and authorization

- `tenant_ctx` is produced only by `RequestContextMiddleware` /
  `resolve_tenant_context` from the verified token and optional verified host
  mapping. A request body/query cannot set tenant, actor, user, brand, KB, or
  `scope_key` (`apps/api-server/src/ragos_api/scope.py`).
- `kb_id` comes from the route path, then `require_kb(...)` checks tenant scope
  and `tenant_ctx.allowed_kb_ids` (`repositories/kb.py:40-44`). The KB is never
  taken from the body.
- `actor_id` and roles come from `AuthClaims`; `require_tenant_admin` is the
  route gate (`auth/deps.py:30-31`). A future membership/ACL resolver must set
  `allowed_kb_ids`; today it is `None`, which is dev-only tenant-wide behavior.
- The worker reconstructs scope from the authoritative `Task` and
  `DocumentVersion` rows, not from arbitrary task payload. It must narrow
  `allowed_kb_ids` to the version's KB (`build_worker_tenant_ctx` gains an
  optional `allowed_kb_ids` argument).
- Artifact references are canonical tenant keys
  (`tenant/<tenant_uuid>/<kind>/<sha256>.bin`) and artifacts are content
  addressed (`storage/artifacts.py:18-31,100-135`).

### 3.3 Existing vs proposed states (explicit)

**Existing persisted states (reused unchanged; no migration):**

- `DocumentRecord.ingest_status`: `pending` (default), `indexed` (written only
  by publication), `failed` (legacy/repair path in migration `0011`, and the
  guarded unpublished failure projection below). These are the only values this
  design reads or writes.
- `ChunkManifest.indexing_state`: `pending`, `indexing`, `indexed`, `failed`
  (plus legacy `unversioned` from migration `0010`).
- `Task.status`: `pending`, `retryable`, `leased`, `completed`, `failed`,
  `cancelled`, `timed_out` (`models/app.py:322-326`).
- `Task.cancel_requested_at`: the durable cancellation request flag, already
  present. It is a request, not a terminal state.

**No proposed document projection and no `cancelled` document value.** An
earlier draft added a free-form `DocumentRecord.ingest_status = "cancelled"`.
That is removed: terminal cancellation already lives on `Task`, and a second
persisted cancellation value would create a duplicate state machine whose truth
could disagree with the task row. **Cancellation is derived from existing
`Task` rows only.** Adding a document-level cancellation state later would be a
separate domain decision with its own reviewed migration; it is out of scope
here.

The guarded unpublished failure projection is retained because `failed` already
exists: a worker may write `failed` with
`WHERE active_index_id IS NULL AND ingest_status <> 'indexed'`, so a concurrent
publication always wins. Retry does **not** reset the document row; phase
precedence below gives active work priority over the historical `failed` value.

**Derived API phases (never persisted).** Rows are evaluated top to bottom; the
first match wins. Active task work always outranks a historical `failed`
document value, and committed publication always outranks stale task rows.

| # | Phase | Derivation (selected version `V`, document `D`) |
|---|---|---|
| 1 | `indexed` | `D.active_index_id` is set, `D.ingest_status == "indexed"`, the active manifest is `indexed`, and `reconciled_at` is present |
| 2 | `empty` | No `DocumentVersion` for `D` |
| 3 | `cancelling` | Any relevant task for `V` is active (`pending`/`retryable`/`leased`) and has `cancel_requested_at` set |
| 4 | `cancelled` | No active relevant task, no published index, and at least one relevant task is terminal `cancelled` (this is the sole source of `cancelled`) |
| 5 | `indexing` | An `ingest_document` task for `V` is `leased`, or the manifest is `indexing` |
| 6 | `preparing` | A `prepare_document` task is `leased` and no manifest exists |
| 7 | `queued` | A `prepare_document` task is `pending`/`retryable` and no manifest exists |
| 8 | `failed` | No active relevant task, no published index, and `D.ingest_status == "failed"`, or the manifest is `failed`, or the latest relevant task is terminal `failed`/`timed_out` |
| 9 | `prepared` | A manifest exists and an `ingest_document` task is `pending`/`retryable`, or the manifest is `pending` with no leased task and no failed terminal |

A **relevant task** for version `V` is a `prepare_document` task whose payload
names `V`, or an `ingest_document` task whose manifest belongs to `V`. An
**active** task is `pending`, `retryable`, or `leased`.

`attention_required` is an orthogonal boolean: true when an active task’s lease
or deadline has elapsed, or when persisted rows disagree (e.g., task
`completed` but no manifest, or phase 1 publication facts with a stale active
task). It never triggers automatic rework; the next action is
`operator_reconcile`.

---

## 4. Proposed HTTP contract

All routes live under the canonical `/tenant/knowledge-bases` router and
therefore inherit `require_tenant_admin`, the middleware’s tenant resolution,
and the single `RagOSError` handler
(`apps/api-server/src/ragos_api/errors.py:97-126`).
Errors use the existing shape:

```json
{"error":{"code":"...","message":"...","details":{}}}
```

No new error class is required. “Capability unavailable” uses
`ValidationError` (422) with a stable `details.reason`, matching the existing
legacy ingest route. A later decision may add a 503 domain error, but this
design does not.

### 4.0 Canonical route namespace (one-batch migration)

Current `main` mounts the KB router at the legacy singular abbreviation
`/tenant/kb` (`routes/kb.py:19-23`). The repository route rule requires plural
resource nouns and no lasting double protocol, so this contract fixes the
canonical namespace **before** adding any new route:

- Canonical prefix: `/tenant/knowledge-bases`. Child resources keep kebab-case
  nouns: `/documents`, `/document-uploads`, `/index-status`,
  `/indexes/{index_id}/pages/{page_no}`.
- **One batch, no alias.** The same change updates the router prefix, every
  in-repo caller and test, and the docs exception that currently preserves the
  old prefix (`docs/development-constraints.md:40`). No redirect, no dual
  mounting, no compatibility wrapper.
- In-repo consumers found at design time are only `routes/kb.py` and
  `tests/test_kb_route.py`; there is no `apps/web`. Before release the PR author
  must confirm no external consumer depends on `/tenant/kb`. If one does, it
  gets a dated, tested migration note; it does not get a permanent alias.
- The baseline facts in §1–§2 remain accurate descriptions of `main` before
  this batch. They are evidence, not a compatibility promise. Every path in
  §4.1–§4.7 below is already canonical.

### 4.1 Route table

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/tenant/knowledge-bases/{kb_id}/document-uploads` | Upload one PDF and enqueue preparation | `202` new; `200` idempotent replay |
| `GET` | `/tenant/knowledge-bases/{kb_id}/documents/{document_id}/index-status` | Read derived durable status | `200` (including `empty`) |
| `POST` | `/tenant/knowledge-bases/{kb_id}/documents/{document_id}/index-status/retry` | Retry the latest retryable terminal work | `202` new; `200` already retried |
| `POST` | `/tenant/knowledge-bases/{kb_id}/documents/{document_id}/index-status/cancel` | Request cancellation of active work | `202` requested; `200` already cancelled |
| `GET` | `/tenant/knowledge-bases/{kb_id}/documents/{document_id}/indexes/{index_id}/pages/{page_no}` | Read one frozen source page | `200` |

Why a separate upload command instead of extending
`POST /tenant/knowledge-bases/{kb_id}/documents`:

- The existing route is JSON metadata creation with a `201` response and no
  idempotency. Changing its media type and response would break callers. The
  one-batch rename in §4.0 changes only the path prefix, not this behavior.
- A single upload command can atomically create the document, register the
  immutable version, and enqueue the preparation task under one idempotency
  key. This avoids a second, unprotected document-creation step.
- The command path is still a KB sub-resource; no new service or permission
  partition is introduced.

### 4.2 Upload request

```
POST /tenant/knowledge-bases/{kb_id}/document-uploads?title=...&external_id=...
Authorization: Bearer <token>
Idempotency-Key: <12-128 chars, [A-Za-z0-9_-]>
Content-Type: application/pdf
X-Request-Id: <optional>

<raw PDF bytes>
```

- **Raw body, not multipart.** `python-multipart` is not a dependency in
  `pyproject.toml`; raw `application/pdf` keeps the import surface unchanged and
  lets the browser send `fetch(file)` directly. No form parser is added.
- `title` is required, 1–255 characters after trim, no control characters. It
  is user metadata, never an authorization input.
- `external_id` is optional, ≤255 characters. It is metadata only; it is not a
  uniqueness key and does not authorize anything.
- `Idempotency-Key` is required. Missing or malformed values become the existing
  `RequestValidationError` → 422 path.
- The API reads at most **16 MiB + 1 byte** (`MAX_PDF_BYTES` in
  `parse/pdf_bytes.py:20`). It checks `Content-Type` and the `%PDF-` magic. It
  does **not** run Poppler, count pages, or parse layout. Deep validation is the
  preparation worker’s job.
- Oversized, empty, wrong media type, or wrong magic → `422 VALIDATION_ERROR`
  with a stable `details.reason` (`PDF_TOO_LARGE`, `PDF_EMPTY`,
  `UNSUPPORTED_MEDIA_TYPE`, `INVALID_PDF_MAGIC`). No partial artifact is
  registered in PostgreSQL.
- The upload route never logs bytes or extracted text. It may log IDs, byte
  count, digest, and trace ID.

**Atomicity and ordering:**

1. Read the bounded body.
2. Validate media type, magic, title.
3. `ArtifactStore.put_bytes(kind="source", data=pdf_bytes)` → `(source_ref,
   source_digest)`. This is content-addressed and idempotent; an orphan on a
   later DB failure is safe.
4. Open one DB transaction.
5. Acquire a transaction-scoped advisory lock keyed by the server-derived upload
   idempotency key (see §4.3). This serializes concurrent identical uploads
   before any document row is created.
6. Authorize the KB (`require_kb`), then check the existing task by idempotency
   key:
   - If present and the payload matches the request digest → replay.
   - If present and the digest differs → `409 CONFLICT`.
   - If present but corrupt (unknown kind/payload, missing version) →
     `409 CONFLICT`; never create a second document.
7. If absent: create `DocumentRecord`, register `DocumentVersion` with
   `source_ref`, `source_digest`, `media_type="application/pdf"`, enqueue
   `prepare_document` with payload
   `{"document_version_id": "...", "request_digest": "<sha256>"}`.
8. Commit, then return.

If the process dies after step 3 but before step 8, the artifact is orphaned but
the next retry is safe. If it dies after commit but before the response, the
retry returns the persisted IDs. This is not cross-system exactly-once; it is
idempotent business recovery.

### 4.3 Upload idempotency

- Scope: `tenant_ctx.require_private_scope_key()` + `kb_id` + the client
  `Idempotency-Key`. This prevents one actor’s key from colliding with another
  actor or KB.
- Server task key:
  ```
  "upload:" + sha256(canonical_json({
      "scope": private_scope_key,
      "kb_id": str(kb_id),
      "key": idempotency_key,
  }))
  ```
- Request digest, using `canonical_json` of **typed, named fields**:
  ```
  request_digest = sha256(canonical_json({
      "schema": "ragos-upload-v1",
      "scope": private_scope_key,
      "kb_id": str(kb_id),
      "title": normalized_title,
      "external_id": normalized_external_id,   # null when absent
      "media_type": "application/pdf",
      "source_digest": source_digest,
  }))
  ```
  `canonical_json(value)` is
  `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
  encoded as UTF-8. `normalized_title` is NFC-normalized and trimmed;
  `normalized_external_id` is NFC-normalized and trimmed, or `null` when absent.
  NUL-delimited concatenation is explicitly rejected: arbitrary user strings can
  contain the delimiter, so concatenation is not injective. Named JSON fields
  cannot alias each other even if a value contains `\u0000`.
  The digest binds the idempotency key to the exact bytes and normalized
  metadata without persisting the raw bytes again.
- The same `canonical_json` helper is used for the task key and the retry key.
- The `Task` row is the durable claim:
  `kind="prepare_document"`, exact payload
  `{"document_version_id": "<uuid>", "request_digest": "<64 hex>"}`.
  `TaskRepository.enqueue` already rejects a reused key with different payload
  (`repositories/tasks.py:399-430`).
- Concurrent identical requests are serialized by the advisory lock before
  document creation. This avoids the current `enqueue` savepoint behavior
  leaving two document rows if two transactions race on the unique task key.
  The lock helper should be generalized from
  `TaskRepository.lock_preparation_key` to
  `lock_idempotency_key(operation, key)`; the existing preparation caller is
  migrated in the same change (no compatibility alias).

**Response (new or replay):**

```json
{
  "document_id": "…",
  "document_version_id": "…",
  "task_id": "…",
  "phase": "queued",
  "status_url": "/tenant/knowledge-bases/<kb>/documents/<doc>/index-status?version_id=<version>",
  "replayed": false,
  "trace_id": "…"
}
```

- `202` for a new upload.
- `200` for a same-key/same-digest replay, with `replayed: true` and the current
  projected phase (including `failed`/`cancelled`). A client can always render
  the persisted result without a second POST.
- `409` for same key/different digest or a corrupt claim.
- Headers: `Location: <status_url>`, `X-Request-Id`, `Cache-Control: no-store`.

**Duplicate semantics to state in UI copy:** same key = same business upload;
a new key with the same bytes creates a new document/version. The system does not
silently merge two documents.

### 4.4 Status response

`GET .../index-status?version_id=<optional UUID>` defaults to the newest
`DocumentVersion` by `(created_at DESC, id DESC)` for the document. A
`version_id` from another document is `404`. No version yet returns `200` with
`phase: "empty"`.

```json
{
  "schema_version": "upload-index-status-v1",
  "tenant_id": "…",
  "kb_id": "…",
  "document_id": "…",
  "document_version_id": "…",
  "phase": "preparing",
  "ingest_status": "pending",
  "attention_required": false,
  "next_action": "poll",
  "poll_after_ms": 1000,
  "retryable": false,
  "active_index_id": null,
  "source": {
    "media_type": "application/pdf",
    "source_digest": "…",
    "page_count": null
  },
  "preparation": {
    "task_id": "…",
    "kind": "prepare_document",
    "status": "leased",
    "attempts": 1,
    "error_code": null,
    "lease_until": "…",
    "cancel_requested_at": null,
    "updated_at": "…",
    "worker_trace_id": "worker-…"
  },
  "indexing": null,
  "progress": {
    "planned_chunk_total": null
  },
  "source_page_url_template": null,
  "timestamps": {
    "uploaded_at": "…",
    "updated_at": "…",
    "finished_at": null
  },
  "trace_id": "…"
}
```

Rules:

- `phase` is one of the derived phases in §3.3.
- `ingest_status` is the current persisted coarse document value. For a terminal
  cancellation, `phase` is `cancelled` while `ingest_status` remains the last
  persisted `pending`/`failed` value; there is no document `cancelled` state. A
  client must render the derived `phase`, not infer state from `ingest_status`.
- `preparation` / `indexing` are the latest relevant tasks for the selected
  version; both may be present. They expose only bounded enums and IDs.
- `error_code` is a stable code from the whitelist in §5.4. `message` (if
  included) is a safe user-facing sentence, never a raw exception, SQL, path,
  secret, or other-tenant data.
- `retryable` is server-derived; the UI must not infer it from the phase.
- `progress.planned_chunk_total` is the manifest’s planned count when known.
  There is no per-chunk progress or fabricated percentage.
- `source_page_url_template` is only non-null for an indexed, reconciled active
  manifest:
  `/tenant/knowledge-bases/{kb_id}/documents/{document_id}/indexes/{index_id}/pages/{page_no}`.
- `next_action`: `upload`, `poll`, `open_source`, `retry`, `replace_source`,
  `operator_reconcile`, or `none`.
- `poll_after_ms`: 1000 while non-terminal; `null` for terminal states.
- `trace_id` equals the request’s `X-Request-Id`; `worker_trace_id` is the
  worker correlation string (`worker-<task_id>`), not a secret.
- The handler is read-only: no `session.commit()`, no task enqueue, no artifact
  write, no `ModelRouter` call, no Qdrant call.

### 4.5 Retry

`POST .../index-status/retry?version_id=<optional>`

- Find the latest terminal task for the selected version:
  - `prepare_document` with `document_version_id` in payload, or
  - `ingest_document` whose manifest belongs to the version.
- Reject with `409 CONFLICT` when:
  - an active task already exists (`pending`/`retryable`/`leased`),
  - the document is `indexed` or has an active index,
  - the latest terminal task is `cancelled` (a new upload key is required),
  - the latest `error_code` is non-retryable (§5.4),
  - `attempts >= DEFAULT_MAX_ATTEMPTS` (5).
- Otherwise enqueue exactly one task:
  - If a failed manifest exists: `ingest_document` payload
    `{"manifest_id": "..."}`.
  - Else: `prepare_document` payload
    `{"document_version_id": "...", "request_digest": "..."}`.
  - Task key: `"retry:" + sha256(canonical_json({"scope": private_scope_key,
    "latest_task_id": str(latest.id)}))`. The latest task ID is server-owned, so
    concurrent double-clicks resolve to the same retry task.
- Do **not** rewrite `DocumentRecord.ingest_status` on retry. A historical
  `failed` value may remain while the new task is active; the derived phase
  table in §3.3 gives active work precedence, so the API truthfully reports
  `queued`/`preparing`/`indexing` and returns to `indexed` only on publication.
  Never touch an `indexed` document.
- Return `202` with the status projection; a concurrent replay returns `200`.

### 4.6 Cancel

`POST .../index-status/cancel?version_id=<optional>`

- Find all non-terminal tasks for the selected version:
  - the `prepare_document` task, and
  - `ingest_document` tasks whose manifest belongs to the version.
- If none:
  - already `cancelled` (derived: no active task, no published index, latest
    relevant task terminal `cancelled`) → `200`;
  - `indexed` or active index present → `409`;
  - otherwise → `409` “nothing to cancel”.
- Otherwise set `cancel_requested_at` on each active task in one transaction
  (the existing `TaskRepository.request_cancel` semantics). The worker’s
  `ProcessRunner` cancellation callback observes the flag, kills the process
  group, and the parent records `cancelled` (`runtime/tasks.py:226-231,277-307`).
- **Do not write a document cancellation state.** The API persists no
  `cancelled` value and does not mutate `DocumentRecord` on cancel. Cancellation
  truth is the `Task` row: `cancel_requested_at` while active, then the terminal
  `cancelled` status. §3.3 derives `cancelling`/`cancelled` from those rows.
- If a publication committed concurrently, the worker’s fenced publication
  wins; no document row is overwritten and the status truthfully remains
  `indexed`. The cancel response is `409`.
- Cancellation is a request, not a rollback of a published index.

### 4.7 Source-page reader

`GET .../indexes/{index_id}/pages/{page_no}`

- Authorize the KB/document, then require a `ChunkManifest` with
  `tenant_id`, `document_id`, and `index_id` matching the path,
  `indexing_state == "indexed"`, and `reconciled_at` present. Historical
  indexed manifests remain readable so old citations do not break when a new
  active index is published.
- Read the frozen representation artifact via `manifest.representation_id` /
  `representation_ref` and `representation_digest`; validate the digest.
- Return one page:

```json
{
  "document_id": "…",
  "document_version_id": "…",
  "representation_id": "…",
  "index_id": "…",
  "page_no": 1,
  "page_count": 3,
  "text": "…",
  "start_char_idx": 0,
  "end_char_idx": 42,
  "parser_version": "poppler-pdftotext-layout-v1",
  "render_version": "none",
  "source_page_url": "/tenant/knowledge-bases/<kb>/documents/<doc>/indexes/<index>/pages/1"
}
```

- Out-of-range page → `404` (authorized resource, missing page). Unindexed
  manifest → `409`. Wrong tenant/document/index → `404`.
- No PDF bytes are served, no Poppler runs, no model is called. This is the
  target of citation page links.

---

## 5. Asynchronous phases and worker contract

### 5.1 Phase flow

```text
POST document-uploads
  -> source artifact + DocumentRecord + DocumentVersion + prepare_document Task
  -> [prepare_document worker]
       source read -> Poppler parse -> frozen representation -> representation artifact
       -> DocumentRepresentation -> frozen embedding route resolution
       -> IndexPlan -> IndexPreparationService.prepare
       -> ChunkManifest + ingest_document Task
  -> [ingest_document worker]
       source/plan/representation validation -> PG FTS upsert -> Qdrant upsert
       -> independent PG/vector readback -> manifest reconciled
       -> document active_index_id + ingest_status=indexed
  -> GET index-status (derived)
  -> GET index page (frozen representation)
```

Preparation and indexing are separate tasks on purpose:

- Poppler and plan construction are CPU/file-heavy and must not hold DB locks
  or run in the API process.
- Indexing is already recoverable and fenced; a parse failure must not create a
  half-indexed manifest.
- A retry can choose the correct phase: re-prepare a failed parse, or re-index a
  prepared manifest.

### 5.2 New task kind `prepare_document`

- Payload exactly `{"document_version_id": "<uuid>", "request_digest": "<64 hex>"}`.
- Worker command:
  `python -m ragos.runtime.prepare_job --tenant-id ... --task-id ... --attempt ...`
- The job claims/fences the task, validates the kind and exact payload, loads
  the `DocumentVersion`, and narrows the worker `TenantContext` to
  `allowed_kb_ids=[version.kb_id]`.
- All DB snapshots happen before artifact/Poppler work; no transaction is held
  across byte I/O or parsing.
- It calls `IndexPreparationService.prepare(plan, idempotency_key=f"task:{task_id}",
  timeout_seconds=...)`, which creates the manifest and the existing
  `ingest_document` task. The preparation retry key is the task ID, so a retry
  of the same preparation task cannot create a second manifest.
- Success: exit 0; the parent `_record_terminal` marks the task `completed`.
- Typed domain failure: mark the document `failed` if unpublished, mark the task
  `failed` with the typed code, then exit 0 so the parent does not overwrite the
  typed code. If the task cannot be marked under its fence, let the parent
  record the generic failure.
- Unexpected failure: mark the document `failed` if unpublished, log only IDs,
  and re-raise. The parent records `TASK_PROCESS_FAILED`.
- Cancellation: before and after `prepare`, check the task’s
  `cancel_requested_at`. If the parent was cancelled after it enqueued the
  child, request cancellation on the child too. If publication already
  committed, the final state is truthfully `indexed`.

### 5.3 Existing `ingest_document` changes

- Keep payload exactly `{"manifest_id": ...}` and the existing
  `IndexPreparationService` / `IngestionService` contracts.
- Replace the test/hash-only gate with an explicit route selector:
  - `test/hash`: only `app_env == "test"` and `task_test_embedding=True` and the
    frozen hash identity; no change in behavior.
  - `dev/local gateway`: only explicit local opt-in, loopback API, Linux worker,
    and a resolved tenant embedding route (§6).
  - `production`: refuse until the gates in §8. No fallback.
- Narrow the worker context to the manifest’s KB.
- On typed failure, mark the document `failed` if unpublished and mark the task
  with the typed code. `IngestionService` already marks the manifest `failed`
  when it can do so under the fence (`ingest.py:289-302`).
- Never mark a document `failed` if the manifest/index publication already
  committed.

### 5.4 Typed task error codes

`Task.error_code` is persisted by `_finish` only if the code is in the
repository whitelist (`repositories/tasks.py:22-45`). Extend that whitelist with
bounded worker codes:

| Code | Retryable | User-facing meaning |
|---|---|---|
| `SOURCE_INVALID_PDF` | no | The file is not a readable PDF |
| `SOURCE_ENCRYPTED` | no | The PDF is protected |
| `SOURCE_NO_TEXT` | no | A page has no extractable text; OCR/review required |
| `SOURCE_TOO_LARGE` | no | PDF input/text/page budget exceeded |
| `SOURCE_PAGE_LIMIT` | no | PDF exceeds the page limit |
| `SOURCE_READ_FAILED` | yes | The immutable source could not be read |
| `EMBEDDING_PROFILE_UNAVAILABLE` | no | The tenant embedding binding is missing/invalid |
| `EMBEDDING_CALL_FAILED` | yes | The model call failed transiently |
| `INDEX_PLAN_CONFLICT` | yes | The active index changed during preparation |
| `INDEX_READBACK_FAILED` | yes | PG/vector readback disagreed; manifest retained |
| `INDEX_PUBLISH_CONFLICT` | yes | Publication CAS lost; no partial publish |
| `INGEST_ISOLATION_UNAVAILABLE` | no | The worker isolation capability is unavailable |

`TASK_*` codes remain the infrastructure fallback. The API maps these codes to
`retryable` and `next_action`; the UI never guesses.

---

## 6. Real-gateway embedding boundary (confirmed blocker)

### 6.1 Why the current gate is insufficient

`ingest_job.py` constructs `QdrantVectorIndexWriter(..., allow_test_embedding=True)`
only for the frozen hash identity. `IndexPlan.embedding_profile/version` is
carried into `ChunkManifest`, and `IngestionService` fails closed if the writer’s
identity differs (`ingest.py:193-199`). That is the correct identity check, but
there is no production writer: the hash function is not semantic, and the local
probe is an operator-only bridge (`local_embedding_probe.py:1-5,71-128`).

### 6.2 Proposed interface

Add a small worker-side gateway seam (runtime module), not a second router. It
reuses the existing `FrozenEmbeddingSpec` (`runtime/local_embedding_probe.py:28-69`)
and the trusted server-side local model pins; it does **not** invent a new
identity schema in `ModelProfile.parameters`. It lives in `ragos.runtime`
because `runtime` already imports `model_gateway` and `storage`, so no
`model_gateway → runtime` reverse dependency is introduced.

```python
# packages/runtime/src/ragos/runtime/embedding_route.py
@dataclass(frozen=True, slots=True)
class FrozenEmbeddingRoute:
    profile: str                  # FrozenEmbeddingSpec.profile
    version: str                  # FrozenEmbeddingSpec.version
    dimension: int
    model_name: str
    provider_slug: str
    spec: FrozenEmbeddingSpec     # provenance; route.version == spec.version

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def resolve_embedding_route(
    *,
    router: ModelRouter,
    tenant_ctx: TenantContext,
    settings: Settings,
) -> FrozenEmbeddingRoute:
    """Authorize the tenant's 'embed' binding; freeze identity from trusted pins."""
```

Rules:

- **Authorization is the tenant binding.** `resolve_embedding_route` calls
  `ModelRouter.resolve(tenant_ctx, "embed")` with **no request-controlled
  profile override**. A tenant without an `embed` binding fails
  `EMBEDDING_PROFILE_UNAVAILABLE`; the platform pins alone do not authorize a
  tenant. This is the existing tenant model-binding path, not a new permission
  partition.
- **Identity material is trusted server-side only.** The frozen identity is
  constructed from the existing operator-attested settings pins:
  - `FrozenEmbeddingSpec(model=settings.local_model_embed,
    model_digest=settings.local_model_embed_revision,
    dimension=settings.local_model_embed_dimension)`.
  - `spec.version` is the identity contract: model-name digest + immutable
    12–64 hex revision + dimension. A mutable alias or `latest` fails
    validation inside `FrozenEmbeddingSpec`.
- **`ModelProfile.parameters` stays empty for the embed use case.** The existing
  `ModelRouter.resolve` already raises
  `ProviderError("Local embedding parameters are not admitted")` when an
  admitted local embed profile carries parameters (`router.py:84`). This design
  keeps that invariant and does **not** add
  `embedding_profile`/`embedding_version`/`embedding_dimension` to
  `parameters`. Identity never travels through arbitrary JSON.
- **Cross-check the resolved binding.** The resolver requires all of:
  - resolved provider is the admitted local provider
    (`local_model_provider_id`),
  - resolved `model_name == spec.model` (the pinned embed model),
  - resolved `parameters` is empty,
  - `spec.dimension` equals the admitted
    `local_model_embed_dimension`.

  Any mismatch fails `EMBEDDING_PROFILE_UNAVAILABLE`; there is no fallback and no
  provider call.
- `provider_slug` comes from the resolved route (the local admission slug). The
  adapter does not decrypt key-pool rows or invent a second local provider.
- `embed(texts)` batches bounded inputs (16 texts / 8,000 chars per text for the
  local admission; a server-owned production batch limit later), calls
  `router.embed` once per batch, and validates:
  - exactly one vector per input,
  - exact dimension,
  - finite numbers, no booleans, at least one non-zero value.
  Any violation is `ProviderError` and fails the task; no partial Qdrant write.
- **Preparation** resolves the route (no provider call) to build the plan with
  `spec.profile` / `spec.version` / `spec.dimension`. **Indexing** resolves it
  again and passes it to the writer. If the two resolutions differ, the
  manifest identity check (`ingest.py:193-199`) fails closed before any write.
- The writer’s `EmbeddingFn` remains the per-text seam; add an optional batch
  `EmbeddingBatchFn` to `QdrantVectorIndexWriter.upsert` so the gateway can
  amortize calls. `reconcile` never embeds and never calls a model.
- Persisting the frozen identity in the control plane (typed column/table) is a
  separate reviewed contract, not part of this design; see §12.1.

### 6.3 Worker startup and execution gates

The ingest worker must refuse to start or claim work unless all applicable
conditions hold:

1. `sys.platform == "linux"` for real execution. `test` may use in-process
   service tests, but the process path is not claimed.
2. `app_env` is `test` with explicit hash opt-in, or `dev` with explicit local
   gateway opt-in, or a future production profile with §8 gates. No implicit
   mode.
3. The worker is the dedicated ingest process, not the API process.
4. The isolation mode is explicit:
   - `none`: allowed only in `test` (and never called “isolated”);
   - `process`: dev-only lifecycle isolation via `ProcessRunner`; not a
     security boundary;
   - `container`/`vm`: the only production-eligible mode, verified by the
     operator’s deployment, not by a Python check.
5. The resolved embedding route’s profile/version/dimension exactly matches the
   prepared manifest before any Qdrant write.
6. No ambient secret or model URL is inherited. The worker command builds an
   explicit environment from server settings, as the current test worker does
   (`ingest-worker/main.py:46-55`), and never copies arbitrary env.

### 6.4 macOS truthfulness

`ProcessRunner.run` raises `ValidationError("process group task runner requires
Linux")` on Darwin (`process_runner.py:86-87`). Therefore:

- A macOS API + UI smoke can prove only HTTP/status rendering and in-process
  service behavior. It **cannot** prove Poppler execution in the worker,
  process-group cancellation, lease heartbeats, or index publication.
- The worker startup must refuse real execution on Darwin; it must not silently
  run parsing in the API process as a fallback.
- Browser acceptance for the upload journey requires a Linux worker host
  (container/VM). macOS results must be labeled “API-only, worker not executed.”

---

## 7. Isolation of Poppler and untrusted parsing

- **API process:** validates media type/magic/size and stores bytes. It must not
  import or invoke `extract_pdf_bytes`, `pdftotext`, or any layout parser. The
  API deployment should not install Poppler.
- **Worker process:** reads the source artifact, runs Poppler via
  `subprocess.run` with `shell=False`, an absolute operator-configured
  executable, `DEVNULL` stdio, the existing 20 s timeout, 16 MiB input,
  8 MiB text, and 200-page bounds (`pdf_bytes.py:26-123`). No network, no
  password bypass, no shell.
- **Production isolation profile (not implemented by this design):** a dedicated
  Linux container/VM running as non-root with read-only root, tmpfs `/tmp`,
  CPU/memory/pids limits, no host mounts except the scoped artifact volume, an
  egress allowlist for PostgreSQL/Qdrant/model endpoints, seccomp/AppArmor, and
  a pinned Poppler package. `ProcessRunner` remains lifecycle isolation only; it
  does not provide cgroups, namespaces, or network isolation.
- **Shared artifacts:** the API and worker must share the scoped artifact root
  (a mounted volume for dev/container) or use the same object-store adapter.
  The worker must not read arbitrary host paths; it uses `ArtifactStore` with
  the tenant prefix and digest validation.
- **First worker PR honesty:** implement the ingest-worker startup refusal and
  the real-gateway admission; do not claim sandboxing. The preparation process
  boundary and Poppler execution arrive in the next slice. The `container` mode
  is an operator deployment requirement, not a code proof.

---

## 8. Security and production-enablement gates

The upload route is part of the tenant API, but production enablement is blocked
until all of the following are real:

1. **Signed identity:** JWT signature/issuer/audience/expiry verification (or an
   equivalent session), not the unsigned `dev.` token. Current
   `decode_bearer_token` accepts only `dev.` and does not verify a signature.
2. **Membership and resource ACL:** a server-side membership/role source; the
   resolver must derive roles and `allowed_kb_ids` from it. Today
   `resolve_tenant_context` trusts token claims and leaves `allowed_kb_ids=None`.
3. **Secret resolution and budget:** a secret resolver that injects provider
   credentials per call without logging them, plus a hard pre-authorized budget
   ledger that fails closed. Today ordinary providers are not connected and
   `KeyPool.api_key_encrypted` is not decrypted/forwarded.
4. **Runner isolation:** the Linux container/VM profile in §7, including
   no-network parsing and egress policy.
5. **Frozen embedding identity:** an operator-attested model digest/version and
   dimension, with readback. No mutable alias or “latest.”
6. **No production startup bypass:** `app_env` outside `dev`/`test` still refuses
   at API startup until 1–5 are satisfied. The upload route returns the closed
   capability error rather than silently working in dev mode.

### 8.1 Local dev opt-in (explicit, loopback, truthful)

A local journey is allowed only when all are true:

- `RAGOS_APP_ENV=dev`
- `RAGOS_API_HOST` is `127.0.0.1`, `localhost`, or `::1`
- `RAGOS_LOCAL_MODEL_ENABLED=true` with the existing local admission fields
  (provider UUID, loopback `/v1` base URL, pinned chat/embed names,
  dimension, operator revision)
- a new explicit `RAGOS_INGEST_LOCAL_GATEWAY=true` for the worker (slice 1)
- a new explicit `RAGOS_INGEST_ISOLATION_MODE=process` (slice 1; dev only, not a
  security boundary)
- a new explicit `RAGOS_LOCAL_UPLOAD_UI=true` for the UI page (slice 4 only)
- the worker runs on Linux

The UI and docs must display, persistently:

> Local development only. Dev bearer tokens are unsigned and can claim any
> role. Do not upload customer or confidential documents. The worker process is
> not a security sandbox. This is not production authorization.

The UI must not silently bypass `require_tenant_admin`: the operator supplies a
dev token, and the page calls the real API with `Authorization: Bearer ...`.
There is no server-side “assume admin” path.

---

## 9. UI proposal (slice 5, after the backend contract is real)

There is no `apps/web`. The smallest honest surface is a **single dev-only HTML
page served by the API process**, not a full admin UI:

- `GET /ui/uploads` returns static HTML + inline JS (no new template
  dependency). It is 404 unless the dev/loopback/flag conditions in §8.1 hold.
- The page has:
  - a dev-token input (sessionStorage, not localStorage; cleared on tenant
    switch),
  - KB ID and document ID inputs (minimal; no KB admin UI),
  - a file input + title field + upload button,
  - a status panel (phase, persisted status, task IDs, error, retry/cancel),
  - a page reader (page number input + text panel).
- It calls the real routes with `fetch`; it does not call services directly and
  does not create a second permission path.
- It displays a persistent dev-only warning.
- It never fabricates progress. `queued`/`preparing`/`indexed` come from the
  server DTO; there is no simulated token/progress animation.
- It stops polling on terminal states, resumes on reload from the server, and
  clears displayed data when the token/KB changes.
- It handles 401/403/404/409/422/network errors with readable text and recovery
  actions.

### 9.1 Required browser acceptance (Linux worker host)

| # | Journey | Acceptance |
|---|---|---|
| B1 | Success | Upload a public synthetic 2-page PDF; observe `queued → preparing → prepared → indexing → indexed`; open pages 1 and 2; text matches the frozen representation; page URL is the citation target |
| B2 | Empty | New document with no version shows `empty` and an upload action |
| B3 | Loading | While active, the page shows the persisted phase and a bounded poll; no fake percentage |
| B4 | Unauthorized | Missing/other-tenant/end-user token shows 401/403; no document data |
| B5 | Failed | Invalid/encrypted/blank PDF shows `failed` with a non-retryable message |
| B6 | Retry | A transient failure shows `retry`; retry creates one task; double-click does not duplicate |
| B7 | Cancel | Cancel during preparation/indexing shows `cancelling → cancelled`; no active index |
| B8 | Reconnect | Reload mid-processing restores the server state and polling; no duplicate upload/task |
| B9 | Duplicate | Same idempotency key returns the same IDs; a second click cannot create a second document |
| B10 | Tenant switch | Changing token/KB clears old data and ignores in-flight responses |
| B11 | Source page | A citation page link opens the exact page text/span, not a different version |
| B12 | Accessibility | Labels, keyboard focus, readable errors, and a mobile viewport pass a manual check |

Evidence: screenshots, network log, status JSON, worker logs with task/trace IDs.
No customer data. macOS-only runs are labeled “API/UI only, worker not executed.”

---

## 10. Implementation ownership and finite PR slices

### 10.1 File ownership (proposed, by slice)

| Slice | Path | Change | Owner | Depends on |
|---|---|---|---|---|
| 1 | `packages/runtime/src/ragos/runtime/embedding_route.py` | New: `FrozenEmbeddingRoute` + resolver (reuses `FrozenEmbeddingSpec`) | Worker | `ModelRouter`, local admission, settings pins |
| 1 | `packages/runtime/src/ragos/runtime/ingest_job.py` | Modify: gateway selector, typed errors, KB-narrowed context | Worker | embedding route |
| 1 | `apps/ingest-worker/src/ragos_ingest_worker/main.py` | Modify: Linux/isolation preflight, explicit env | Worker | ingest job, embedding route |
| 1 | `packages/storage/src/ragos/storage/vector.py` | Modify: optional batch embedding seam | Storage | gateway route |
| 1 | `packages/storage/src/ragos/storage/repositories/tasks.py` | Modify: typed worker code whitelist | Storage | models |
| 1 | `packages/storage/src/ragos/storage/repositories/kb.py` | Modify: guarded unpublished `failed` projection only | Storage | models |
| 1 | `packages/core/src/ragos/core/config.py` | Modify: explicit ingest dev flags (no secrets) | Core | — |
| 1 | `tests/test_ingest_gateway.py` | New: gateway boundary/batch validation/Linux refusal/identity mismatch | Tests | worker |
| 2 | `apps/api-server/src/ragos_api/routes/kb.py`, `tests/test_kb_route.py`, `docs/development-constraints.md` | One-batch `/tenant/kb` → `/tenant/knowledge-bases` migration | Backend | slice 1 |
| 2 | `apps/api-server/src/ragos_api/routes/uploads.py`, `apps/api-server/src/ragos_api/main.py` | New upload route + registration | Backend | upload service |
| 2 | `packages/runtime/src/ragos/runtime/upload.py` | New: authorize, artifact write, document/version creation, task claim | Backend | repos, artifacts, tasks |
| 2 | `packages/storage/src/ragos/storage/repositories/tasks.py` | Modify: `lock_idempotency_key` (generalize `lock_preparation_key`) | Storage | models |
| 2 | `tests/test_upload_route.py` | New: upload/idempotency/negative cases | Tests | route/service |
| 3 | `packages/runtime/src/ragos/runtime/prepare_job.py` | New: preparation worker CLI (Poppler, representation, plan) | Worker | parser, index preparation, embedding route |
| 3 | `apps/ingest-worker/src/ragos_ingest_worker/main.py` | Modify: register `prepare_document` | Worker | prepare job |
| 3 | `tests/test_prepare_job.py` | New: Poppler/plan/idempotency/typed failure | Tests | worker |
| 4 | `apps/api-server/src/ragos_api/routes/uploads.py` | Modify: status/retry/cancel/page routes | Backend | projection service |
| 4 | `packages/runtime/src/ragos/runtime/upload.py` | Modify: status projection, retry/cancel, page read | Backend | repos |
| 4 | `packages/storage/src/ragos/storage/repositories/kb.py` | Modify: page read helpers | Storage | models |
| 4 | `tests/test_upload_status.py`, `tests/test_source_page_route.py` | New: phase/retry/cancel/no-side-effect; page reader | Tests | service |
| 5 | `apps/api-server/src/ragos_api/routes/uploads.py` (or a small `ui.py`) | New: dev-only `/ui/uploads` page | Backend | slices 2–4 |
| 5 | browser acceptance per §9 | Manual/Playwright evidence | Frontend/QA | Linux worker |

No new Python package, no `pyproject.toml` dependency change, no migration, no
new service. If the page reader or route helpers need a new module inside an
existing package, register nothing new.

### 10.2 First finite implementation PR (slice 1 only)

**Title:** `feat(s1): isolated Linux ingest worker with real-gateway embedding`

**In scope:**

1. `FrozenEmbeddingRoute` built from `FrozenEmbeddingSpec` + trusted server-side
   local pins, authorized by the tenant’s real `embed` binding. Embed
   `ModelProfile.parameters` stay empty; no request/task identity input.
2. `ingest_job.py` explicit route selector:
   - `test/hash` unchanged (`app_env=test` + `task_test_embedding=True` + frozen
     hash identity);
   - `dev/local gateway` only with explicit opt-in, loopback API, Linux worker,
     and a resolved tenant route (§6.3);
   - `production` refuses. No fallback.
3. Explicit child environment built from server settings; no ambient secrets,
   no inherited model URL.
4. Linux + isolation-mode preflight and macOS refusal; `none` never called
   isolated; no sandbox claim.
5. Optional batch embedding seam with strict validation (count, dimension,
   finite/non-bool/non-zero); manifest profile/version/dimension identity check
   before any Qdrant write; `reconcile` never embeds.
6. Guarded unpublished `failed` document projection and the typed worker codes
   needed by this slice (`EMBEDDING_PROFILE_UNAVAILABLE`,
   `EMBEDDING_CALL_FAILED`, `INGEST_ISOLATION_UNAVAILABLE`,
   `SOURCE_READ_FAILED`).
7. Tests G1–G6 and the indexing cases I1–I3/I5 from §11.3, with skips when the
   environment is absent; Linux CI evidence plus one real local-gateway worker
   run with PG/Qdrant readback.
8. PR description states the exact worker command, pinned model identity,
   isolation mode, and the fact that upload/status/UI are not implemented.

**Out of scope for slice 1 (explicit):**

- No upload route, no `prepare_document`, no status/retry/cancel, no source-page
  reader (slices 2–4).
- No route-namespace migration (slice 2), no UI (slice 5).
- No training, SFT/GRPO, multimodal/OCR, workflow DSL, plugin system, full admin
  UI, scheduler, sandbox runner, or multi-service split.
- No production auth/membership, secret decryption, budget ledger, or production
  deployment.
- No real customer/enterprise PDF or gold; no semantic quality claim.
- No deletion/retention/GC of orphan artifacts, no reindex/backfill, no Qdrant
  server restart/backup work.
- No change to the existing chat/retrieval/rerank behavior.

**Acceptance for slice 1:** on a Linux host, an `ingest_document` task prepared
with the frozen route embeds through the real local gateway, writes PG + Qdrant,
readback reconciles, and the manifest becomes `indexed`; the test/hash path
still behaves exactly as before; production and non-Linux real execution refuse;
no upload, status, or UI capability is claimed. macOS service-level smoke is
labeled “API/service only, worker not executed” and is not acceptance.

**Dependent slices (each requires the previous to be accepted):**

- **Slice 2:** canonical `/tenant/knowledge-bases` migration + PDF upload
  command + idempotency.
- **Slice 3:** `prepare_document` worker (Poppler, representation, plan).
- **Slice 4:** derived index-status, retry/cancel, source-page reader.
- **Slice 5:** dev-only `/ui/uploads` page and browser acceptance in §9. No
  admin UI.

---

## 11. Test matrix

Layers: **R** = route/TestClient with fakes, **P** = PostgreSQL integration
(disposable `RAGOS_TEST_PG_DSN`), **Q** = in-memory or local Qdrant, **L** =
Linux + Poppler worker, **B** = browser (slice 5). A missing environment is a
skip, never a pass claim.

Slice mapping: **G1–G6 + I1–I3/I5 = slice 1 acceptance**; U1–U12 = slice 2;
P1–P5 = slice 3; S1–S14, R1–R5, C1–C5, L1–L5, A1–A4 = slice 4; B1–B12 = slice
5. A test may land earlier only if it is inert without its slice; it may not be
counted as acceptance for unimplemented behavior.

### 11.1 Upload and authorization

| ID | Layer | Case | Expected |
|---|---|---|---|
| U1 | R/P | New PDF upload | `202`, one document/version/task, source artifact exists, exact task payload |
| U2 | R/P | Same key + same digest replay | `200`, same IDs, `replayed=true`, no new rows/artifact |
| U3 | R/P | Same key + different bytes/title | `409 CONFLICT`, no new document |
| U4 | R/P | Same bytes + new key | New document/version (explicit, not merged) |
| U5 | R | Missing/invalid `Idempotency-Key` | `422 REQUIRED` / `INVALID_VALUE` |
| U6 | R | Wrong media type / no `%PDF-` / empty | `422`, reasons `UNSUPPORTED_MEDIA_TYPE` / `INVALID_PDF_MAGIC` / `PDF_EMPTY` |
| U7 | R | Body > 16 MiB | `422 PDF_TOO_LARGE`; no DB rows |
| U8 | R | No token / end-user role | `401` / `403` |
| U9 | R | KB from another tenant | `404` (no existence leak) |
| U10 | R | Same tenant, KB outside `allowed_kb_ids` | `403` (repository-level negative; production ACL source is still a blocker) |
| U11 | R | Document in another KB | `404` for status/page/retry |
| U12 | R | Client body/query attempts `tenant_id`/`kb_id`/`scope_key` | `403` / no scope change |

### 11.2 Status, retry, cancel, reconnect

| ID | Layer | Case | Expected |
|---|---|---|---|
| S1 | R/P | No version | `200`, `phase=empty`, `next_action=upload` |
| S2 | R/P | Prepare task pending/retryable | `queued` |
| S3 | R/P | Prepare task leased | `preparing` |
| S4 | R/P | Manifest + ingest task pending | `prepared` |
| S5 | R/P | Ingest task leased / manifest indexing | `indexing` |
| S6 | R/P/Q | Published manifest | `indexed`, page template non-null, `retryable=false` |
| S7 | R/P | Invalid PDF terminal | `failed`, bounded non-retryable code, `next_action=replace_source` |
| S8 | R/P | Transient index failure | `failed`, `retryable=true`, `next_action=retry` |
| S9 | R/P | Cancel requested but leased | `cancelling`; after terminal `cancelled` |
| S10 | R/P | Status GET side effects | No task/model/Qdrant call; no row mutation; same response on repeat |
| S11 | R/P | Reconnect after page reload | Same persisted phase/IDs; no new task |
| S12 | R/P | Expired lease/deadline | `attention_required=true`, `next_action=operator_reconcile`; no auto-retry |
| S13 | R/P | `version_id` from another document | `404`; default selects the newest version |
| S14 | R/P | Terminal cancel, no published index | phase `cancelled`, `retryable=false`; persisted `ingest_status` remains `pending`/`failed` (no document cancel value) |
| R1 | R/P | Retry while active | `409` |
| R2 | R/P | Retry non-retryable failure | `409`; no task |
| R3 | R/P | Retry transient prepare/index | `202`, exactly one task with `retry:<latest>` key |
| R4 | R/P | Concurrent double retry | Same retry task returned; no duplicate |
| R5 | R/P | Retry at attempt cap (5) | `409`; no task |
| C1 | R/P | Cancel pending prepare | `202`; task terminal `cancelled`; no index; `DocumentRecord.ingest_status` unchanged (no document cancel value) |
| C2 | R/P | Cancel leased indexing | `cancelling`; guard prevents publication; task terminal `cancelled`; no document cancel write |
| C3 | R/P | Cancel after publication | `409`; published index remains; status `indexed` |
| C4 | R/P | Cancel twice | Second call `200`; no new work |
| C5 | R/P | Cancel one document | Other document/tasks unchanged |

### 11.3 Worker, parsing, indexing, fencing

| ID | Layer | Case | Expected |
|---|---|---|---|
| P1 | L/P | Synthetic 2-page PDF preparation | Real Poppler; representation artifact; manifest; `ingest_document` task; document still pending |
| P2 | L/P | Blank/encrypted/invalid PDF | Typed `SOURCE_*` code; no manifest; document `failed`; task `failed` |
| P3 | L/P | Preparation retry | Same manifest/task; no duplicate representation/manifest |
| P4 | L/P | Source artifact tampered | `ConflictError`/digest failure; manifest `failed`; no publish |
| P5 | L/P | Plan/representation mismatch | Fail closed before manifest/publication |
| I1 | P/Q | Indexing success | PG + Qdrant readback digests equal; manifest `indexed`; document `indexed`; page spans correct |
| I2 | P/Q | One-sided write/readback failure | Manifest `failed`; no active index; document `failed`; retry works |
| I3 | P/Q | Stale worker after lease reclaim | Old attempt cannot mark complete/publish; new attempt wins |
| I4 | P/Q | Worker crash/timeout | Task `failed`/`timed_out`; no auto model replay; status `attention_required` |
| I5 | P/Q | Cancel before publish | Execution guard rejects; no active index |
| I6 | P/Q | Cancel after publish | `409`; index remains readable |
| G1 | R | Gateway batch count/dimension/NaN/zero | `ProviderError`/typed failure; no Qdrant write |
| G2 | R | Route profile/version mismatch | Fail closed before any write |
| G3 | R | Child env contains no ambient secret | Only explicit server-owned keys; test pattern extended |
| G4 | R | Real worker on Darwin | Refuses at startup/run; no API fallback |
| G5 | R | Production/unknown profile without gateway | Worker refuses to start or claim; no fallback capability |
| G6 | R | `ProcessRunner` on Darwin | Raises `ValidationError`; this is not worker acceptance |

### 11.4 Source page and audit

| ID | Layer | Case | Expected |
|---|---|---|---|
| L1 | R/P | Authorized indexed page | `200`; text/span equals frozen representation; correct IDs/parser |
| L2 | R/P | Page 0 / out of range | `404` (or `422` for non-positive path); no other page leak |
| L3 | R/P | Unindexed/failed manifest | `409`; no text |
| L4 | R/P | Other tenant/document/index | `404` |
| L5 | P/Q | Historical indexed manifest after a newer active index | Old page still readable; citation link stable |
| A1 | R/P | Trace IDs | Response `X-Request-Id` == status `trace_id`; worker `worker-<task_id>`; task/attempt IDs correlate |
| A2 | R/P | No model replay on GET/retry validation | Monkeypatched model/worker raise if called; status still 200; retry only enqueues |
| A3 | R/P | Error redaction | No raw PDF text, SQL, paths, secrets, other-tenant IDs in `error.details` |
| A4 | P | Immutable identity | Document/version/manifest/index IDs stable across retries; no duplicate business fact |

### 11.5 Frozen audit/trace identifiers

- `X-Request-Id`: incoming header or server UUID; returned on every API
  response; status DTO `trace_id` equals it.
- Worker `trace_id`: `worker-<task_id>` (existing pattern in `ingest_job.py`).
- Persisted correlation: `tenant_id`, `document_id`, `document_version_id`,
  `index_id`/manifest ID, `task_id`, `attempts`.
- Logs: IDs, digest prefixes, byte counts, safe error code; never raw PDF text,
  raw title, SQL, paths, secrets, or other-tenant data.
- The `Task` row is the durable attempt audit for this slice; `ChunkManifest` is
  the durable index audit. No separate audit table is added.

### 11.6 Rollback scope

- **Slice 1 code:** revert `embedding_route.py`, the `ingest_job.py` selector,
  the worker preflight, the batch seam, and the new typed codes; the worker
  returns to the test/hash-only gate and real execution refuses. Manifest
  failure projection may be left in place (it writes an existing value).
- **Slice 2–4 code:** revert the upload/status/page modules and the plural-route
  migration in one batch (restore `/tenant/kb` only together with all callers);
  the API returns the closed-capability error. No schema migration to reverse.
- **Config:** disable `RAGOS_INGEST_LOCAL_GATEWAY` (slice 1) and
  `RAGOS_LOCAL_UPLOAD_UI` (slice 5); stop the worker. In-flight tasks are fenced
  and can be cancelled.
- **Data:** do not delete source/representation/manifest/task rows or artifacts.
  Published indexes remain readable; old citations remain valid. A bad index is
  handled by a future shadow/reindex/active-pointer procedure, not deletion.
- **Production:** no slice produces a production deployment, so rollback is
  local/dev only. If a later migration is added, it needs its own backup and
  restore plan.

---

## 12. Blocking decisions and available resources

### 12.1 Blocking decisions (unresolved)

1. **Production auth:** signed JWT/session vs another mechanism; membership and
   KB ACL schema. Owner: auth/tenant contract.
2. **Secret resolver and budget ledger:** how to decrypt/inject provider keys
   without logging, and how to pre-authorize/settle ingestion model cost.
3. **Isolation implementation:** container vs VM; cgroup/network/seccomp
   profile; artifact volume and egress allowlist. Owner: infra/runtime.
4. **Frozen embedding identity storage:** slice 1 keeps the identity in trusted
   server-side settings (`FrozenEmbeddingSpec` + local model pins) and leaves
   `ModelProfile.parameters` empty, because `ModelRouter.resolve` rejects
   non-empty embed parameters. Whether to persist a typed frozen-identity
   column/table for the control plane is a separate contract decision before
   production; it must not be smuggled through arbitrary JSON parameters.
5. **Retry budget:** whether a user-initiated retry may spend model budget, the
   max attempts (currently 5), and the UI copy. No budget ledger exists.
6. **Cancel-after-publication:** this design returns `409` and never rolls back
   a published index. Confirm this product rule.
7. **Document cancellation state:** this design deliberately adds none;
   `cancelling`/`cancelled` are derived from existing `Task` rows
   (`cancel_requested_at`, terminal `cancelled`). Whether a future coarse
   document-level cancellation state is needed is a separate domain decision
   with its own migration; it is not required for this journey.
8. **Closed-capability error:** this design reuses `ValidationError` 422 with a
   stable reason, matching the existing legacy ingest route. A 503 domain error
   is a possible later contract change.
9. **UI placement:** dev-only page in `ragos_api` vs a future `apps/web`.
   Slice 1 does not depend on this.
10. **Adding a version to an existing document:** out of scope; the upload
    command creates a document + first version. A later versioning flow needs
    its own idempotency contract.

### 12.2 Available resources (honest)

- Public `main` at `d6e74f2`, existing tests/fixtures, synthetic PDF generator,
  in-memory Qdrant, and disposable PostgreSQL fixtures if the operator supplies
  `RAGOS_TEST_PG_DSN`.
- Poppler 26.10.0 and Docker Model Runner local Qwen on the operator Mac, per
  the public local-acceptance record. That record is a **synthetic local smoke**
  included in merged PR27, not this upload journey and not production
  capability.
- **No authorized enterprise/customer PDF or human page-level gold exists.**
  No semantic retrieval/citation quality is claimed.
- No Linux worker image in the repo; no cgroup/network isolation; no signed
  auth; no membership/ACL; no secret resolver/budget; no production deployment.

### 12.3 Prior acceptance provenance (separate, not conflated)

Two different pieces of evidence exist and must not be merged into one claim:

| Item | State | What it actually shows | What it does not show |
|---|---|---|---|
| PR27, included in `main` `d6e74f2` | **Merged** | Local synthetic Chat/PDF/in-memory-Qdrant smoke from `docs/validation/s1-local-model-acceptance-2026-10-23.md`: real local Qwen Embedding/Chat, synthetic PDF + Poppler, in-memory Qdrant, structural citation check. Targeted evidence was 46 passed/3 skipped; the separate local suite run was 1041 passed/41 skipped. No GitHub Actions run was triggered for that local smoke. | Upload, preparation worker, durable index status, UI, or production readiness. |
| PR28 at head `b93797d` | **Open, not merged** | Real `ModelRouter` / published-PostgreSQL / Qdrant acceptance; Linux Full CI `39907662440` succeeded with **1084 passed / 1 intentional real-model skip**. | Merge status, upload/worker journey, UI, or production readiness. |

This design does not consume or upgrade either claim. Slice 1 must produce its
own worker-level evidence. It does not depend on PR28's unmerged test code; if
PR28 merges first, slice 1 rebases onto the resulting `main`. The earlier draft
incorrectly described the merged PR27 smoke as a “published-model acceptance
pending its own PR”; that statement is withdrawn.

---

## 13. Review gates before implementation

1. Contract owner approves §3–§4 (records/states/routes/DTOs), including “no
   new persisted state value” and the canonical plural route batch.
2. Security owner approves §6–§8 (gateway identity provenance, isolation, auth
   gates) and the dev-only opt-in wording.
3. Worker owner approves §5, §6.2–§6.3, and the Linux/preflight behavior for
   slice 1.
4. Frontend owner accepts §9 scope and the browser matrix for slice 5.
5. The integration owner freezes slice 1 file ownership and its test matrix.
6. Only then does slice 1 implement code. Upload/status routes and UI remain
   later dependent slices; the design itself is not evidence of a working
   journey.

---

## 14. Revision history (2026-10-23 review)

This revision addresses six review items and changes no product code:

1. **Embedding identity:** removed `ModelProfile.parameters` as the identity
   carrier; reuse `FrozenEmbeddingSpec` + trusted server-side local pins,
   authorize the tenant’s real `embed` binding, compare to the manifest, and
   keep embed parameters empty.
2. **Routes:** canonical `/tenant/knowledge-bases`, one-batch migration of
   `/tenant/kb` and all callers/tests/docs; baseline paths retained as
   pre-migration facts, no alias.
3. **First PR:** frozen to slice 1 only (isolated Linux ingest worker,
   real-gateway embedding, acceptance tests); upload/preparation/status/UI are
   later slices 2–5.
4. **Request digest:** `canonical_json` of typed named fields instead of
   NUL-delimited concatenation; title/external_id normalization defined.
5. **Cancellation:** no persisted document `cancelled` state; `cancelling` /
   `cancelled` derived from existing `Task` rows only, with no document mutation
   on cancel.
6. **Evidence:** corrected provenance — PR27 merged in `main` is the local
   synthetic smoke; PR28 (`b93797d`, open) is the real ModelRouter/PG/Qdrant
   acceptance with Linux CI 1084 passed / 1 intentional real-model skip. Neither
   proves this journey.
