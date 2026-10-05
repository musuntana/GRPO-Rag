# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RagOS is a multi-tenant AI workflow and RAG SaaS platform built as a **modular monolith with isolated workers**. The primary architecture document is `docs/architecture.md` (currently v1.2); read it before making structural changes. The implementation roadmap is in `docs/implementation.md`. Market context and per-rule provenance are in `docs/competitive-survey.md`, and cross-package constraints are recorded as ADRs in `docs/adr/`.

## Commands

```bash
# Setup
make install-dev          # Create venv + install with dev deps
make up                   # Start PostgreSQL, Qdrant, Valkey via Docker Compose
make migrate              # Run Alembic migrations (alembic upgrade head)

# Development
make api                  # Start API server (ragos-api, auto-reload in dev)
make worker               # Start workflow worker (ragos-workflow-worker)
make ingest-worker        # Start ingest worker
make train-worker         # Start offline reranker-training worker
make lint                 # ruff check apps packages tests
make test                 # pytest -q

# Run a single test
.venv/bin/activate && pytest tests/test_middleware.py -q
.venv/bin/activate && pytest tests/test_middleware.py::test_name -q

# Docker infra
make down                 # Stop dev containers
```

## Architecture

### Monorepo Layout

- **`apps/`** — Thin process entrypoints. Currently: `api-server` (FastAPI), `workflow-worker` (LangGraph executor), `ingest-worker`, `train-worker`. Target adds: `scheduler-worker`, `sandbox-runner`, `web`.
- **`packages/`** — Shared business logic. Currently: `core`, `auth`, `tenant`, `runtime`, `storage`, `model-gateway`, `retrieval`, `memory`. Target adds: `control-plane`, `toolhub`, `sandbox`, `observability`.
- **`docs/`** — `architecture.md` (source of truth), `implementation.md` (milestones), `competitive-survey.md` (market intelligence, informational), `adr/` (accepted decisions).
- **`infra/`** — Docker Compose (`infra/docker/docker-compose.dev.yml`) and Alembic migrations (`infra/migrations/`).

### Import Mapping

Filesystem directories use kebab-case (`apps/api-server`, `packages/model-gateway`). Python imports come from the package under `src/`. The mapping is defined in `pyproject.toml [tool.setuptools.package-dir]`:

| Filesystem path | Python import |
|---|---|
| `apps/api-server/src/ragos_api/` | `ragos_api` |
| `apps/workflow-worker/src/ragos_workflow_worker/` | `ragos_workflow_worker` |
| `packages/core/src/ragos/core/` | `ragos.core` |
| `packages/auth/src/ragos/auth/` | `ragos.auth` |
| `packages/tenant/src/ragos/tenant/` | `ragos.tenant` |
| `packages/storage/src/ragos/storage/` | `ragos.storage` |
| `packages/runtime/src/ragos/runtime/` | `ragos.runtime` |
| `packages/model-gateway/src/ragos/model_gateway/` | `ragos.model_gateway` |
| `packages/retrieval/src/ragos/retrieval/` | `ragos.retrieval` |
| `packages/retrieval/src/ragos/retrieval/query/` | `ragos.retrieval.query` |
| `packages/memory/src/ragos/memory/` | `ragos.memory` |

When adding a new subpackage (e.g. `ragos.storage.repositories`), it must be registered both in `[tool.setuptools.packages]` and `[tool.setuptools.package-dir]` in `pyproject.toml`. Don't rename directories to make them importable.

### API & Route Structure

The API server (`ragos_api.main`) creates the FastAPI app via `create_app()`, attaches `RequestContextMiddleware`, registers error handlers, and includes routers. Routes are in `apps/api-server/src/ragos_api/routes/`:

- `platform.py` — `/platform/tenants` CRUD (platform admin, cross-tenant)
- `platform_users.py` — `/platform/tenants/{id}/users` (platform admin)
- `tenant.py` — `/tenant/context` (tenant-scoped)
- `users.py` — `/tenant/users` CRUD (tenant admin)
- `kb.py` — `/tenant/kb` CRUD + document upload + ingest trigger
- `search.py` — `/tenant/search/query` (tenant-scoped full-text search)
- `tasks.py` — `/tenant/tasks` (tenant-scoped task management)
- `model_center.py` — `/platform/model-center/*` (providers, key-pools, profiles)
- `model_bindings.py` — `/tenant/model-bindings` (tenant model binding CRUD)
- `retriever_profiles.py` — `/tenant/retriever-profiles` (versioned retrieval configuration)
- `retrieval.py` — `/tenant/retrieval/query` (the read path end to end, with the explain attached)
- `chat.py` — `/chat/conversations`, `GET|POST .../messages` (SSE), and per-message feedback. Its authorization is *authenticated + a resolvable `app.user` row*, deliberately not `require_tenant_admin` — an end user is the actor here

### Data Plane

PostgreSQL uses **three schemas** in one database:
- `app` — control plane metadata, tenant/user/config, workflow defs, run summaries
- `search` — lexical retrieval (tsvector/GIN), tables: `chunk_stable`, `chunk_hot`, `memory`
- `checkpoint` — LangGraph checkpoints only

Qdrant collections are organized by **embedding profile, not by tenant**. Tenant isolation uses payload filters. Never create one collection per tenant.

Valkey/Redis is for task dispatch, pubsub, locks, and rate limiting — never as a durable source of truth.

### Key Patterns

**Tenant Context** — Every tenant-scoped request resolves a `TenantContext` (Pydantic model in `ragos.core.tenant`) before business logic runs. The middleware (`ragos.tenant.middleware.RequestContextMiddleware`) populates `request.state.tenant_ctx`. Use `get_tenant_ctx()` dependency in routes. Thread IDs for LangGraph are `{tenant_id}:{workflow_id}:{run_id}`, derived from `scope_key` rather than accepted from the caller.

**Retrieval Chain** — `ragos.runtime.retrieval.RetrievalService` resolves a stored `retriever_profile_version` row, builds the components that profile asks for, and runs `RAGPipeline`. Recall runs on `QueryPlan.queries` (one call per query, deduplicated on `chunk_id`); grading, refinement, and generation always run on `QueryPlan.primary`, because grading a HyDE hypothesis against a hypothesis measures nothing. `ragos.retrieval.refine` is the single sentence-level refine node — all three CRAG outcomes pass through it, and `ambiguous` re-retrieves once with the refined sentence, merging new chunks with the first pass.

**Memory** — `ragos.memory` is architecturally separate from the knowledge base; do not let a fact and a passage end up in the same context block. `MemoryFactStore` is bi-temporal: a contradicting fact sets `valid_until` on its predecessor and points `superseded_by` at itself rather than overwriting the text, and `as_of()` answers point-in-time questions from the interval. `decay_score` is derived from access recency *inside* the validity window — a fact outside its window scores zero however often it was read, and `touch()` resets recency without reviving a closed fact. Every memory operation takes `scope_key` **and** `user_id`; `TenantContext.require_user_id()` refuses without an actor, and `ragos.tenant.resolver.resolve_actor_user_id` maps the authenticated subject to their `app.user` row (not in the middleware — it is a per-request query most requests do not need). `RollingSummarizer` requires `window_n` plus the `memory_profile_version` that recorded it; there is no default. Writeback is a durable `memory_writeback` task keyed on `{conversation_id}:{message_count}`, run by `make_memory_writeback_handler`.

**Run Events** — `ragos.runtime.events.EventLog`/`EventSink`. Each run gets an append-only JSONL file at `{storage_root}/{scope_key}/runs/{run_id}/events.jsonl`; ids are the line number, so they are monotonic and per-run. `emit` persists *then* queues, so an acknowledged event is already on disk. `stream(last_event_id=…)` replays the missed range and then follows live events without repeating any. Paths are percent-encoded from `scope_key`, never character-substituted. Payloads above `MAX_INLINE_EVENT_BYTES` are offloaded beside the log and referenced by path. There is deliberately no compaction routine — one that trims a run's events is one bad interaction away from deleting what a suspended run needs.

**Chat Turns** — `ChatService.run_turn` emits `run_started` → `memory_loaded` (only when there is memory) → `retrieval_completed` → `rerank_completed` → `relevance_graded` → `token_delta`* → `citation_linked` → `memory_writeback_scheduled` → `run_finished`. It returns `None` rather than an iterator, because a generator can be consumed once and the events have to survive a disconnect. Two honest deviations: there is **no** `memory_updated` event (the writeback is asynchronous, so the event would assert something that has not happened), and `token_delta` is segmentation of a finished answer, not token streaming, because the provider interface returns a complete message.

**Recall Channels** — `lexical` and `hot` are PostgreSQL-backed (`SearchRepository`, `ts_rank_cd`); `dense` is Qdrant + a gateway embedding; `vision` uses a separate collection. The Qdrant payload carries no `body`, so dense recall returns ids and scores and hydrates text from `search.*` through `SearchRepository.get_chunks_by_ids` — a vector with no row is dropped, and a Qdrant outage raises rather than returning zero hits. Every vector read builds its filter with `ragos.runtime.vector.build_filter`; `DenseRecall` accepts an injectable `filter_builder` so the tenant predicate can be asserted without the optional `qdrant-client` driver. `ragos.runtime.retrieval.build_recall_channels` wires what a deployment can serve and names the collection per embedding profile, never per tenant.

**Parent Expansion** — `SearchParentResolver` fetches parents and `SearchRepository.count_children` in one tenant scope. The sibling *denominator* is required: the majority-sibling rule compares selected children against a total, and without it the rule silently degrades to "emit every referenced parent". A child whose parent is not emitted is kept as a leaf — the rule deduplicates pages, it must not delete the span the retriever found.

**Retriever Profiles** — `app.retriever_profile_version` is append-only; a write creates a new version rather than editing one, and `RetrieverProfileRepository.create_version` derives the version number from stored rows instead of the request. `ragos.retrieval.profile.profile_from_row` validates a row before it is served and fails closed rather than rescaling invalid budgets. `RetrievalService` refuses to serve a profile whose requested capabilities are missing (no entailment callable, for instance) and reports unwired recall channels as response warnings.

**Scope Key (v1.1, partially implemented — remaining M1 work)** — `scope_key` is `{tenant_id}:{workspace_id}:{user_id}`, composed once in trusted server code and stored server-side. A repository or service called without a `scope_key` raises; it must never widen to an unscoped query. See `docs/adr/0005-server-derived-scope-key.md`. Done: `SearchRepository` and `RetrieverProfileRepository` take `scope_key` as a required parameter of every method. Not yet done: the generic repositories in `repositories/base.py`, and caller-supplied scope fields in the older routes — `ragos_api.scope.enforce_body_scope` (used by the retrieval routes) is the pattern to extend.

**Fail-Closed Budgets (v1.1)** — In-flight spend is pre-authorized before dispatch and settled after. If the authoritative balance cannot be read, the request is rejected with `503`, not admitted. Every fallback hop re-checks model entitlement and remaining budget. Rate limiting is the deliberate exception. See `docs/adr/0001-budgets-and-identity-fail-closed.md`.

**Two-Axis Sandbox (v1.1)** — Execution isolation and network isolation are separate configurations with separate enforcement points. Isolation tiers never downgrade silently; an unavailable tier fails closed. Network policy is enforced at the sandbox runner's egress boundary and the HTTP egress proxy, never per HTTP library. See `docs/adr/0003-sandbox-two-axis-no-silent-downgrade.md`.

**POST-Only Resume (v1.1)** — Human-input resume links require an explicit `POST`. `GET`/`HEAD` renders a confirmation page and must not mutate workflow state, because mail security scanners prefetch every URL. See `docs/adr/0002-resume-links-require-post.md`.

**Own Price Book (v1.1)** — LiteLLM is a library for provider normalization, not a proxy and not the billing authority. `key_pool`, `tenant_binding`, quota enforcement, and the versioned cost table are ours. See `docs/adr/0004-own-price-book-rent-transport.md`.

**RLS Defense-in-Depth** — `get_db_session()` in `ragos_api.dependencies` calls `SET LOCAL app.current_tenant_id` on every session when a tenant is resolved (via `request.state.rls_tenant_id`). This is a secondary guard; application code must still scope queries via `tenant_ctx`.

**Error Hierarchy** — `RagOSError` base in `ragos.core.errors` with subclasses mapped to HTTP status codes. Raise domain exceptions in services, never in handlers. A single FastAPI exception handler (`ragos_api.errors`) maps them to `{"error": {"code", "message", "details"}}`.

**Dependency Aliases** — `ragos_api.dependencies` exports `DBSession`, `TenantCtx`, `AuthCtx` as `Annotated` types for use in route signatures.

**Repository Pattern** — `ragos.storage.repositories.base` provides `TenantScopedRepository` (all queries filtered by `tenant_ctx.tenant_id`) and `PlatformRepository` (unscoped, for platform admin use). All domain repos inherit from one of these. Repositories live in `packages/storage/src/ragos/storage/repositories/`.

**Model Gateway** — every LLM call in the retrieval chain (generation, rails grading, query rewriting, HyDE, decomposition, refinement) goes through `ragos.retrieval.gateway` adapters onto `ragos.model_gateway.router.ModelRouter`, and each adapter takes `TenantContext` so the call is metered and budget-checked. There is no path from a retrieval feature to a provider client. Provider implementations live in `ragos.model_gateway.providers/` (currently `stub.py`). Key rotation via `ragos.model_gateway.key_pool.KeyPoolManager`. All LLM/embedding calls must go through the model gateway. Fallback is classified by error type (`content_policy`, `context_window_exceeded`, provider/key exhaustion, then cross-model), and per-run cost is recorded as `{retrieval_tokens, generation_tokens, enrichment_tokens, memory_tokens, rerank_tokens, tool_ms, sandbox_ms}`.

**Auth** — Development uses `Bearer dev.<base64url-json>` tokens parsed in `ragos.auth.jwt`. RBAC checks via `ragos.auth.rbac` (`require_tenant_admin`, `require_platform_admin`). Auth claims extracted via `ragos.auth.deps.get_auth_claims`.

### Configuration

All settings use `RAGOS_` env prefix via `pydantic-settings` (`packages/core/src/ragos/core/config.py`). Settings are cached via `get_settings()` (`@lru_cache`). Key settings: `RAGOS_PG_DSN`, `RAGOS_QDRANT_URL`, `RAGOS_VALKEY_URL`, `RAGOS_STORAGE_ROOT`, `RAGOS_APP_ENV` (set to `dev` for auto-reload).

### Testing

Tests are in `tests/` and use `httpx.AsyncClient` with `create_app(testing=True)` to bypass lifespan (no real DB/Qdrant needed). The app factory's `testing=True` flag skips engine creation and checkpointer setup, making unit tests fast and self-contained.

## Non-Negotiable Rules (from AGENTS.md)

1. No microservices — one codebase, thin process roles.
2. No tenant data access without `tenant_ctx`.
3. No direct storage queries from HTTP handlers — go through service and repository layers.
4. No one-collection-per-tenant in Qdrant.
5. No PostgreSQL as a high-frequency event bus or token-log sink.
6. No script execution in the API or worker main process.
7. All provider calls through the model gateway — no direct provider calls from feature code.
8. Never trust a caller-supplied scope identifier; derive `scope_key` server-side and discard `tenant_id` / `brand_id` / `kb_id` / `user_id` / `model_binding_id` from payloads.
9. Fail closed on unverifiable tenant scope, budget, or secrets. Rate limits are the exception.
10. Never silently downgrade sandbox isolation, and never apply network policy inside individual HTTP libraries.
11. Human-input resume requires POST; GET/HEAD renders a confirmation page only.
12. LiteLLM is a library, not a proxy; the price book and cost ledger are ours.
13. Read `docs/architecture.md` before making large design changes. If a change contradicts it, update the doc first — and update `docs/competitive-survey.md` and `docs/adr/` in the same change when the change was driven by market evidence or constrains more than one package.

## Tooling

- **Python ≥ 3.11** (pyproject.toml says 3.11, implementation guide targets 3.12+)
- **Ruff** for linting: line-length 100, target py311, selects E/F/I/UP/B
- **pytest** with tests in `tests/` directory
- **Alembic** for migrations, config in `alembic.ini`, scripts in `infra/migrations/`
- **uv** lockfile present (`uv.lock`)

## API URL Conventions

- `/platform/*` — platform admin (cross-tenant, requires `platform_admin` role)
- `/tenant/*` — tenant admin (scoped by `tenant_ctx`, requires `tenant_admin` role)
- `/chat/*` — end user (scoped by `tenant_ctx` + `user_id`)

Plural nouns for collections, kebab-case for multi-word paths. Return 201 for creates, 200 for updates, 204 for deletes.
