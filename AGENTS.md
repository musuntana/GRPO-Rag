# AGENTS.md

## Mission

Build this repository as a multi-tenant AI workflow and RAG SaaS using a modular monolith with isolated workers.

Optimize for:

- tenant isolation
- reproducibility
- observability
- low operational complexity
- a clean later path to service extraction

`docs/architecture.md` is the primary architecture source of truth.

## Non-Negotiable Guardrails

1. Do not introduce microservices by default. Start with one codebase and thin process roles.
2. Do not allow tenant-scoped data access without `tenant_ctx`.
3. Do not query storage directly from HTTP handlers. Go through service and repository layers.
4. Do not create one Qdrant collection per tenant.
5. Do not use PostgreSQL as a high-frequency event bus or token-log sink.
6. Do not execute Script nodes in the API process or worker main process.
7. Do not store binary assets or verbose traces in PostgreSQL.
8. Do not mix enterprise knowledge retrieval with long-term user memory retrieval.
9. Do not bypass the model gateway by calling providers directly from arbitrary feature code.
10. Do not add new top-level patterns that contradict `docs/architecture.md` without updating the architecture doc first.

## Working Rules For Agents

1. Read `docs/architecture.md` before making large design or structural changes.
2. Keep process entrypoints thin. Put reusable logic in shared packages.
3. Prefer explicit schemas, typed interfaces, and versioned configuration objects.
4. Any tenant-facing feature must define how tenant scoping is enforced in storage, retrieval, workflow execution, and secrets.
5. Any long-running or retryable work must have idempotency and durable status tracking.
6. Any new retrieval logic must be profile-driven and evaluable.
7. Any new node or tool type must define timeout, retry, permissions, audit, and UI trace behavior.

## Expected Repository Shape

Target layout:

```text
apps/
  api-server/
    src/ragos_api/
  workflow-worker/
    src/ragos_workflow_worker/
  ingest-worker/
    src/ragos_ingest_worker/
  scheduler-worker/
    src/ragos_scheduler_worker/
  sandbox-runner/
    src/ragos_sandbox_runner/
  web/

packages/
  core/src/ragos/core/
  auth/src/ragos/auth/
  tenant/src/ragos/tenant/
  control-plane/src/ragos/control_plane/
  runtime/src/ragos/runtime/
  retrieval/src/ragos/retrieval/
  memory/src/ragos/memory/
  model-gateway/src/ragos/model_gateway/
  toolhub/src/ragos/toolhub/
  sandbox/src/ragos/sandbox/
  storage/src/ragos/storage/
  observability/src/ragos/observability/

docs/
  architecture.md
```

If the repository is still being scaffolded, create code in a way that converges on this structure.

Source layout conventions:

- filesystem app directories use kebab-case, for example `apps/api-server`
- Python import packages live under `src/` and use normal Python package names, for example `ragos_api`
- do not rename directories just to make them importable; imports come from the package under `src/`, not from the app folder name

Current scaffold snapshot (`2026-04-11`):

- apps created: `api-server`, `workflow-worker`
- packages created: `core`, `auth`, `tenant`, `runtime`, `storage`
- docs should distinguish clearly between current scaffold state and target repository shape

## Tenant Context Contract

Every tenant-scoped request must resolve a canonical `tenant_ctx` before business logic runs.

Minimum fields:

```text
tenant_id
tenant_slug
actor_id
actor_type
roles
plan_tier
locale
timezone
trace_id
```

Additional optional fields:

```text
brand_id
allowed_kb_ids
model_binding_id
security_level
```

Agent rule:

- if a repository or service can read or mutate tenant data without `tenant_ctx`, fix that first

## Storage Rules

### PostgreSQL

Use PostgreSQL for:

- tenant and auth metadata
- configuration
- workflow and prompt versions
- run summaries
- eval datasets
- task envelopes
- structured memory facts
- lexical search tables and indexes

Use one PostgreSQL cluster and one primary database with three schemas:

- `app`: control plane and metadata
- `search`: lexical retrieval tables and indexes
- `checkpoint`: LangGraph checkpoints only

Do not use PostgreSQL for:

- token-by-token logs
- large blobs
- high-frequency append-only trace streams

Use Row-Level Security as defense in depth on high-risk tenant tables. It does not replace `tenant_ctx` enforcement in code.

### Qdrant

Use Qdrant for:

- dense retrieval
- memory vectors
- hot knowledge tier

Rules:

- collections are organized by embedding profile, not tenant
- tenant isolation is enforced with payload filters
- create payload indexes before bulk upload when filters are known
- enable and respect strict mode in production

### Object Storage or Local Files

Use files for:

- raw uploads
- parsed artifacts
- JSONL traces
- exports

Paths must be tenant-scoped.

### Valkey or Redis

Use it only for:

- task dispatch
- short-lived pubsub
- worker leases
- locks
- rate limits
- ephemeral caches

It is not the durable source of truth.

## Workflow Rules

1. Workflow definitions are stored as JSON DSL, not executable Python.
2. DSL is compiled to LangGraph and cached by version.
3. Runtime state must be explicit and typed.
4. Side effects must be wrapped so replay is safe and idempotent.
5. Use resumable execution for human-in-the-loop and long-running flows.
6. Prefer per-invocation subgraphs by default.
7. Only use per-thread subgraphs when state across calls is required.
8. If a per-thread subgraph is exposed as a tool, prevent parallel calls to that same namespace.

## LangGraph Implementation Rules

1. Thread IDs must be tenant-scoped: `{tenant_id}:{workflow_id}:{run_id}`. Pass `tenant_id` via the config `configurable` dict.
2. Use `PostgresSaver` or `AsyncPostgresSaver` in all non-test deploys. Use `InMemorySaver` only for tests and short-lived local prototypes.
3. Call `checkpointer.setup()` at process startup.
4. Use `astream_events(version="v2")` for granular streaming (tokens, node events, tool calls). Use `astream(stream_mode="updates")` for simpler node-level deltas.
5. For human-in-the-loop: compile with `interrupt_before` on designated nodes, resume with `Command(resume=value)` API.
6. Use `RetryPolicy(max_attempts=N, retry_on=[...])` at the node level for transient errors (rate limit, timeout). Do not retry all errors blindly.
7. Use a node registry pattern: map DSL node `type` to handler function via `functools.partial` with the node's config. Keep the compiler generic.
8. Compile subgraphs independently, add them as nodes in parent graphs. Use distinct state schemas per subgraph.
9. Write error info to graph state so conditional edges can route to fallback paths. Add a catch-all `error_handler` node.
10. Never store tenant data in global or module-level Python state.

## Ingestion Rules

1. Default chunking strategy is semantic splitting with parent-child hierarchy: small child chunks (256-512 tokens) for retrieval precision, parent chunks (1024-2048 tokens) for generation context.
2. Apply contextual chunking (Anthropic-style) by default: prepend an LLM-generated document context summary to each chunk before embedding. Store original chunk text separately for display.
3. Use prompt caching when enriching many chunks from the same document.
4. Contextual enrichment is configurable per KB — allow tenants to disable it for cost savings.
5. Batch embedding calls through the model gateway, never call embedding providers directly.
6. Do not pretend Qdrant and PostgreSQL are one transaction. Persist an indexing manifest in PostgreSQL first, perform idempotent upserts to Qdrant and PostgreSQL search tables, and only mark the batch `indexed` after both succeed.
7. Parent-child relationships must be stored in chunk metadata for expansion during retrieval.

## Retrieval Rules

Default retrieval chain:

```text
query rewrite
-> intent classify + query decomposition
-> routing
-> lexical recall (PostgreSQL FTS)
-> dense recall (Qdrant)
-> memory recall (facts + vectors)
-> hot-tier recall (L3)
-> fusion
-> rerank
-> relevance gate (CRAG)
-> generation + citation linking
-> self-consistency check
-> memory writeback (fact extraction)
```

Rules:

- keep long-term memory separate from enterprise KB
- keep hot knowledge physically separate from stable knowledge
- make fusion and rerank profile-driven
- keep citations as first-class runtime data
- evaluate retrieval changes before making them the default
- always run the CRAG relevance gate before generation: grade top-K chunks, re-retrieve on `ambiguous`, refuse on `irrelevant`
- expand child chunks to parent chunks after reranking, before building the generation context
- support HyDE as an optional query strategy for vague/short queries
- support agentic RAG mode (LLM-driven dynamic retrieval) as a LangGraph subgraph with retrieval tools
- default to direct retrieval; enable advanced modes per tenant profile or workflow node config

## Memory Rules

1. Memory is architecturally separate from knowledge base. Do not merge them in storage or retrieval.
2. Use three tiers: working memory (in-graph state, per-run), conversation buffer (recent turns + rolling summaries), long-term memory (extracted facts + vectors, cross-conversation).
3. After each conversation, run fact extraction to produce structured facts with category, confidence, and decay score.
4. Use rolling conversation summarization to compress older turns. Keep recent N turns verbatim, summarize the rest.
5. During RAG, inject memory as a separate context section in the generation prompt — clearly labeled, not mixed with KB evidence.
6. Facts have a `decay_score` that decreases over time and resets on access. Archive or delete low-decay facts periodically.
7. Memory recall is a parallel retrieval channel alongside KB recall, not a sequential pre-filter.

## Streaming Rules

Use SSE for:

- chat token streaming
- workflow event streaming
- debugger views

Rules:

- emit monotonic event IDs
- support reconnect with `Last-Event-ID`
- do not rely on in-memory-only event state
- persist detailed traces outside PostgreSQL

## Security Rules

### HTTP nodes

Must have:

- outbound policy
- secret indirection
- timeout
- retry policy
- SSRF protection
- audit log

### Script nodes

Must run in an isolated sandbox with:

- timeout
- CPU and memory limits
- filesystem isolation
- no network by default
- output validation

### Secrets

Rules:

- secrets are tenant-scoped
- resolve by alias, not raw secret material in workflow defs
- never log raw secrets

## Model Gateway Rules

All provider calls must flow through a model gateway abstraction.

Minimum abstractions:

- provider
- key_pool
- model_profile
- tenant_binding

Every run should record the resolved provider and model profile actually used.

## API Design Conventions

URL structure:

- `/platform/*` — platform admin (cross-tenant, requires `platform_admin` role)
- `/tenant/*` — tenant admin (scoped by `tenant_ctx`, requires `tenant_admin` role)
- `/chat/*` — end user (scoped by `tenant_ctx` + `user_id`)

Rules:

- use plural nouns for resource collections: `/tenant/kb`, `/tenant/workflows`
- use kebab-case for multi-word paths: `/tenant/model-center`
- return `201` for creates, `200` for updates, `204` for deletes
- paginate all list endpoints with `?page=&page_size=` (default page_size=20, max 100)
- return errors as `{"error": {"code": "KB_NOT_FOUND", "message": "...", "details": {}}}`
- SSE endpoints return `text/event-stream` with typed events and monotonic `event_id`
- always include `X-Request-Id` header (same as `trace_id` in `tenant_ctx`)

## Error Handling Conventions

Define a domain exception hierarchy rooted in a base class:

- `RagOSError` (base)
  - `NotFoundError` → HTTP 404
  - `ConflictError` → HTTP 409
  - `ValidationError` → HTTP 422
  - `PermissionDeniedError` → HTTP 403
  - `QuotaExceededError` → HTTP 429
  - `ProviderError` → HTTP 502 (upstream LLM/embedding failure)
  - `SandboxError` → HTTP 500 (script execution failure)
  - `TimeoutError` → HTTP 504

Rules:

- raise domain exceptions in services, never in handlers
- map domain exceptions to HTTP responses in a single FastAPI exception handler
- include `error.code` (machine-readable) and `error.message` (human-readable) in all responses
- never leak stack traces, internal paths, or tenant data from other tenants in error responses
- log full exception details server-side with `trace_id` for correlation

## Observability And Evaluation

Every major feature should be observable in:

- retrieval explain
- workflow trace
- model usage and cost accounting
- evaluation and feedback loops

Rules:

- persist summaries in PostgreSQL
- persist deep traces as JSONL or file artifacts
- version prompts, workflows, retrievers, models, and parsers
- never expose raw private chain-of-thought as a product feature

## Change Acceptance Checklist

A change is not complete unless it covers the relevant items below:

- code follows the repository layout direction
- tenant scoping is explicit and enforced at the repository layer
- persistence choice matches the storage rules (PostgreSQL boundaries, search schema, checkpoint schema, Qdrant payload filters)
- retries and idempotency are defined for async work
- timeouts and auth are defined for external calls
- tracing and audit behavior are defined (OTel spans for LLM/retrieval/tool calls)
- new retrieval logic includes CRAG relevance grading
- new ingestion logic supports contextual chunking and parent-child hierarchy
- memory operations are separated from KB operations
- eval test cases exist for retrieval/generation quality changes (RAGAS metrics)
- unit/integration tests exist for critical behavior
- docs are updated when architecture or behavior changes

## When In Doubt

Choose the simpler design that:

- works on a single host first
- preserves future migration paths
- keeps durable state explicit
- enforces tenant isolation centrally

If a change adds irreversible complexity, stop and update `docs/architecture.md` before proceeding.
