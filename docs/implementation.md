# RagOS Development Implementation Guide

Historical draft: 2026-04-11. Governance notice added: 2026-10-09.

> 本文件保留旧实现思路及示例，不再决定开发顺序。按 [development-roadmap](development-roadmap.md) 的 S0–S5 和 [development-constraints](development-constraints.md) 执行；[architecture](architecture.md) 是架构约束的主事实源。旧 M0–M10、工期估计、示例代码、命令、勾选与“5 passed”等历史记载都不代表当前 HEAD 可运行或已验收。前端随 S1 起的用户旅程交付；安全隔离从 S0 开始，不能按旧 M9/M10 延后。

This historical draft translated an earlier architecture into an illustrative milestone plan. Each milestone has a clear deliverable, lists the exact packages and files to create, defines key interfaces, and specifies acceptance criteria.

Read `docs/architecture.md` for the architecture and `docs/development-roadmap.md` for the current what and when.

---

## Prerequisites

### Runtime

- Python 3.12+
- Docker & Docker Compose (for PostgreSQL, Qdrant, Valkey)
- Node.js 20+ (frontend; Phase 1 can use server-rendered templates)

### Services (Docker Compose)

```yaml
# infra/docker/docker-compose.dev.yml
services:
  postgres:
    image: postgres:16
    ports: ["5432:5432"]
    environment:
      POSTGRES_USER: ragos
      POSTGRES_PASSWORD: ragos_dev
      POSTGRES_DB: ragos
    volumes: ["pg_data:/var/lib/postgresql/data"]
    command: >
      postgres
        -c shared_preload_libraries=pg_stat_statements
        -c log_min_duration_statement=200

  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333", "6334:6334"]
    volumes: ["qdrant_data:/qdrant/storage"]

  valkey:
    image: valkey/valkey:latest
    ports: ["6379:6379"]

volumes:
  pg_data:
  qdrant_data:
```

### Local Data

```text
data/
  storage/         # file storage root (gitignored)
```

## Current Scaffold Status

Snapshot as of `2026-04-11`:

- Implemented apps: `api-server`, `workflow-worker`
- Implemented shared packages: `core`, `auth`, `tenant`, `storage`, `runtime`
- Repository uses `src` layout
  - app code lives under `apps/<kebab-case>/src/<python_package>`
  - shared code lives under `packages/<name>/src/ragos/<python_package>`
- PostgreSQL-first scaffold is in place
  - settings, sync SQLAlchemy engine, and session factory
  - Alembic environment plus `0001_pg_scaffold`
  - `app`, `search`, and `checkpoint` schema initialization
  - LangGraph `AsyncPostgresSaver` bootstrap
- API scaffold is in place
  - `GET /healthz`
  - `RequestContextMiddleware` for `X-Request-Id`, auth claims, and `tenant_ctx`
  - global `RagOSError` JSON handler
  - tenant-scoped `/tenant/context`, `/tenant/tasks`, `/tenant/search/query`
- Verified locally
  - `ruff check apps packages tests` passes
  - `pytest -q tests` passes (`5 passed`)
- Still blocked on local infrastructure verification
  - Docker daemon was not running during the last check
  - `docker compose -f infra/docker/docker-compose.dev.yml up -d` and `alembic upgrade head` still need to be rerun against a live PostgreSQL instance

---

## Milestone Overview

| # | Milestone | Packages Created | Depends On | Est. Effort |
|---|-----------|-----------------|------------|-------------|
| M0 | Project Bootstrap | `core` | — | 1-2 days |
| M1 | Tenant Foundation | `tenant`, `auth` | M0 | 3-4 days |
| M2 | Model Gateway | `model_gateway` | M1 | 2-3 days |
| M3 | Knowledge Base & Ingestion | `storage`, `retrieval` (partial) | M1, M2 | 5-7 days |
| M4 | RAG Pipeline | `retrieval` (complete) | M3 | 5-7 days |
| M5 | Chat & Memory | `memory` | M4, M2 | 4-5 days |
| M6 | Workflow Engine | `runtime`, `toolhub` (partial) | M4, M5 | 5-7 days |
| M7 | Tool Nodes & Security | `toolhub` (complete), `sandbox` | M6 | 3-4 days |
| M8 | Observability & Eval | `observability` | M5, M6 | 3-4 days |
| M9 | Frontend | `web` | M5, M6, M8 | 3-5 days |
| M10 | Hardening & Ship | — | All | 3-5 days |

**Total estimated: ~38-53 days for a small team (1-2 developers)**

### Milestone Dependency Graph

```text
M0 ──> M1 ──> M2 ──> M3 ──> M4 ──> M5 ──> M6 ──> M7
                               |       |       |
                               |       |       └──> M8
                               |       |             |
                               |       └─────────────┤
                               |                     |
                               └──────────────────── ┴──> M9 ──> M10
```

Critical path: **M0 → M1 → M2 → M3 → M4 → M5 → M6 → M10**

M7 (tool security), M8 (observability), M9 (frontend) can be parallelized once their dependencies are met.

---

## M0: Project Bootstrap

**Goal:** Runnable monorepo skeleton with PostgreSQL connection, schema setup, config, and dev tooling.

### Deliverables

```text
ragos/
├── pyproject.toml
├── alembic.ini
├── Makefile
├── AGENTS.md
├── docs/
│   ├── architecture.md
│   └── implementation.md
├── apps/
│   ├── api-server/
│   │   └── src/
│   │       └── ragos_api/
│   │           ├── __init__.py
│   │           ├── dependencies.py
│   │           ├── errors.py
│   │           ├── main.py
│   │           └── routes/
│   │               ├── search.py
│   │               ├── tasks.py
│   │               └── tenant.py
│   └── workflow-worker/
│       └── src/
│           └── ragos_workflow_worker/
│               ├── __init__.py
│               └── main.py
├── packages/
│   ├── auth/
│   │   └── src/ragos/auth/
│   │       ├── __init__.py
│   │       ├── deps.py
│   │       ├── jwt.py
│   │       └── rbac.py
│   ├── core/
│   │   └── src/ragos/core/
│   │       ├── __init__.py
│   │       ├── config.py
│   │       ├── errors.py
│   │       ├── logging.py
│   │       └── tenant.py
│   ├── runtime/
│   │   └── src/ragos/runtime/
│   │       ├── __init__.py
│   │       ├── checkpoints.py
│   │       ├── ingest.py
│   │       ├── search.py
│   │       └── tasks.py
│   ├── storage/
│   │   └── src/ragos/storage/
│   │       ├── __init__.py
│   │       ├── db.py
│   │       ├── models/
│   │       └── repositories/
│   └── tenant/
│       └── src/ragos/tenant/
│           ├── __init__.py
│           ├── middleware.py
│           └── resolver.py
├── infra/
│   ├── docker/
│   │   └── docker-compose.dev.yml
│   └── migrations/
│       ├── env.py
│       └── versions/
│           └── 0001_pg_scaffold.py
├── tests/
│   ├── test_middleware.py
│   └── test_tenant_context.py
└── data/                          # gitignored
    └── storage/
```

### Key Interfaces

```python
# packages/core/config.py
class Settings(BaseSettings):
    pg_dsn: str = "postgresql://postgres:postgres@localhost:5432/ragos"
    pg_app_schema: str = "app"
    pg_search_schema: str = "search"
    pg_checkpoint_schema: str = "checkpoint"
    storage_root: str = "data/storage"
    qdrant_url: str = "http://localhost:6333"
    valkey_url: str = "redis://localhost:6379"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

# packages/storage/db.py
def get_engine(settings: Settings | None = None) -> Engine: ...
def get_session(settings: Settings | None = None) -> Iterator[Session]: ...
def ensure_schemas(engine: Engine | None = None,
                   settings: Settings | None = None) -> None: ...

# packages/runtime/checkpoints.py
def build_checkpoint_dsn(dsn: str, schema: str) -> str: ...
async def langgraph_checkpointer(settings: Settings | None = None) -> AsyncIterator[Any]: ...

# packages/core/errors.py
class RagOSError(Exception):
    code: str
    message: str
    status_code: int = 500
    details: dict[str, Any]

class AuthenticationError(RagOSError):   # 401
    ...
class PermissionDeniedError(RagOSError): # 403
    ...
class ValidationError(RagOSError):   # 422
    ...
class NotFoundError(RagOSError):     # 404
    ...

# apps/api-server/src/ragos_api/main.py
def create_app(*, testing: bool = False) -> FastAPI:
    app = FastAPI(title="RAGOS API", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    ...
    return app
```

### Acceptance Criteria

- [ ] `docker compose -f infra/docker/docker-compose.dev.yml up` starts PostgreSQL, Qdrant, Valkey
  Last verification was blocked because the local Docker daemon was not running.
- [ ] `alembic upgrade head` creates three schemas: `app`, `search`, `checkpoint`
  Last verification was blocked because no PostgreSQL server was listening on `localhost:5432`.
- [x] `ragos-api` scaffold exposes `GET /healthz` → 200
- [x] X-Request-Id middleware: every response includes `X-Request-Id` header
- [x] Exception handler: `RagOSError` subclasses return structured `{"error": {"code", "message", "details"}}` with correct status code
- [x] `pytest -q tests` passes
- [x] `ruff check apps packages tests` passes
- [ ] `mypy packages/` pass clean

---

## M1: Tenant Foundation

**Goal:** Tenant resolution, RBAC, Row-Level Security, and basic platform admin API.

### Current Scaffolded Slice

Already implemented in the scaffold:

- `TenantContext` and `ActorType` live in `packages/core/src/ragos/core/tenant.py`
- `RequestContextMiddleware` resolves `X-Request-Id`, auth claims, and `tenant_ctx` for protected routes
- auth currently supports a local development token format: `Bearer dev.<base64url-json>`
- router-level RBAC is wired for tenant admin routes via `require_tenant_admin`
- `ragos_api.dependencies.get_tenant_ctx()` reads `request.state.tenant_ctx`, not ad hoc headers
- tests cover:
  - unauthenticated request → `401`
  - wrong role → `403`
  - successful tenant context injection
  - request-id echo

Still pending for the full M1 milestone:

- host-header and custom-domain tenant resolution
- platform admin tenant CRUD
- `control_plane` package and tenant/user repositories
- PostgreSQL RLS policies wired into request/session lifecycle

### Deliverables

```text
packages/
  tenant/
    __init__.py
    context.py             # TenantContext frozen dataclass
    middleware.py           # FastAPI middleware: resolve tenant, set tenant_ctx
    resolver.py             # Resolution: host header / custom domain / JWT
  auth/
    __init__.py
    jwt.py                 # JWT encode/decode, API key validation
    rbac.py                # Permission checker
    deps.py                # FastAPI Depends: require_role, require_permission, get_tenant_ctx
  control_plane/
    __init__.py
    models/
      tenant.py            # Tenant, TenantDomain, TenantBrand (SQLAlchemy, schema=app)
      user.py              # User, Role, Permission
    repos/
      base.py              # TenantScopedRepo — enforces tenant_id
      tenant_repo.py
      user_repo.py
    services/
      tenant_svc.py
      user_svc.py
apps/
  api-server/
    src/
      ragos_api/
        routes/
          platform/
            tenants.py     # POST/GET/PUT /platform/tenants
            users.py
```

### Key Interfaces

```python
# packages/tenant/context.py
@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant_slug: str
    brand_id: str | None
    actor_id: str
    actor_type: Literal["user", "api_key", "system"]
    roles: frozenset[str]
    plan_tier: str
    allowed_kb_ids: frozenset[str] | None  # None = all
    model_binding_id: str | None
    locale: str
    timezone: str
    trace_id: str

# packages/control_plane/repos/base.py
class TenantScopedRepo(Generic[T]):
    """For tenant-admin and end-user paths. All queries scoped to tenant_ctx.tenant_id."""
    model: type[T]

    def __init__(self, session: AsyncSession, tenant_ctx: TenantContext):
        self.session = session
        self.tenant_ctx = tenant_ctx

    def _scoped(self, stmt: Select) -> Select:
        return stmt.where(self.model.tenant_id == self.tenant_ctx.tenant_id)

    async def get_by_id(self, id: str) -> T | None: ...
    async def list(self, *, page: int = 1, page_size: int = 20) -> tuple[list[T], int]: ...
    async def create(self, **kwargs) -> T: ...
    async def update(self, id: str, **kwargs) -> T: ...
    async def delete(self, id: str) -> None: ...

class PlatformRepo(Generic[T]):
    """For platform-admin paths only. No automatic tenant scoping.
    Callers must hold platform_admin role (enforced by require_role dep).
    Explicit tenant_id filters are required when querying tenant-scoped data."""
    model: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, id: str) -> T | None: ...
    async def list(self, *, page: int = 1, page_size: int = 20,
                   tenant_id: str | None = None) -> tuple[list[T], int]: ...
    async def create(self, **kwargs) -> T: ...
    async def update(self, id: str, **kwargs) -> T: ...
```

### DB Migration (app schema)

```sql
-- app.tenant
CREATE TABLE app.tenant (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    plan_tier TEXT NOT NULL DEFAULT 'free',
    settings JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- app.tenant_domain
CREATE TABLE app.tenant_domain (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES app.tenant(id),
    domain TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,  -- subdomain, custom
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- app.tenant_brand
CREATE TABLE app.tenant_brand (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES app.tenant(id) UNIQUE,
    logo_url TEXT,
    theme JSONB,
    welcome_text TEXT,
    custom_css TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- app.user
CREATE TABLE app."user" (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES app.tenant(id),
    external_id TEXT,
    name TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'end_user',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, external_id)
);

-- Row-Level Security (defense in depth)
ALTER TABLE app."user" ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app."user"
    USING (tenant_id = current_setting('app.current_tenant_id', TRUE));
-- Set per-request: SET LOCAL app.current_tenant_id = '...';
```

### Acceptance Criteria

- [ ] Tenant resolution works via host header, custom domain, and JWT claim
- [ ] `POST /platform/tenants` creates tenant + brand + default admin user
- [ ] `GET /platform/tenants` lists tenants (platform admin only)
- [ ] All repos use `TenantScopedRepo`; cross-tenant queries impossible from service layer
- [ ] RLS policies active on user table as defense in depth
- [ ] Unauthorized → 401; wrong role → 403
- [ ] Tests: resolution, RBAC enforcement, repo tenant scoping, RLS isolation

---

## M2: Model Gateway

**Goal:** Provider abstraction, key pool rotation, model profile binding, tenant-level configuration.

### Deliverables

```text
packages/
  model_gateway/
    __init__.py
    router.py              # Resolve tenant → profile → provider → key
    key_pool.py            # Weighted round-robin, health, quota
    types.py               # ChatRequest, ChatChunk, EmbeddingRequest, etc.
    providers/
      base.py              # Abstract: chat_stream, embed_batch, rerank
      openai.py
      anthropic.py
  control_plane/
    models/
      model_config.py      # Provider, KeyPool, ModelProfile, TenantModelBinding
    repos/
      model_config_repo.py
    services/
      model_svc.py
apps/
  api-server/
    src/
      ragos_api/
        routes/
          platform/
            model_center.py
```

### Key Interfaces

```python
# packages/model_gateway/router.py
class ModelRouter:
    async def chat(self, request: ChatRequest, tenant_ctx: TenantContext,
                   profile_override: str | None = None) -> AsyncIterator[ChatChunk]:
        """
        Resolve: tenant_binding → model_profile → provider → key_pool → key.
        On failure: try fallback_profile_id.
        Record: provider, model, tokens, latency.
        """
        ...

    async def embed(self, texts: list[str], tenant_ctx: TenantContext,
                    profile_override: str | None = None) -> list[list[float]]: ...

    async def rerank(self, query: str, documents: list[str],
                     tenant_ctx: TenantContext) -> list[RerankResult]: ...

# packages/model_gateway/key_pool.py
class KeyPoolManager:
    async def acquire_key(self, pool_id: str) -> KeyLease: ...
    async def release_key(self, lease: KeyLease, *, success: bool) -> None: ...
    # Unhealthy after N consecutive failures → auto-disable
```

### Acceptance Criteria

- [ ] Platform admin CRUDs providers, key pools, model profiles
- [ ] Tenant binding maps tenant → default chat/embed/rerank profiles
- [ ] Key pool rotates with weighted round-robin; unhealthy keys auto-disabled
- [ ] Fallback: primary failure → automatic switch to fallback_profile_id
- [ ] Every call returns metadata: provider, model, tokens, latency
- [ ] Tests: rotation, fallback, unhealthy detection

---

## M3: Knowledge Base & Ingestion

**Goal:** Document upload, parsing, semantic chunking with contextual enrichment, embedding, dual indexing (Qdrant + PostgreSQL FTS).

### Deliverables

```text
packages/
  storage/
    __init__.py
    file_store.py          # Tenant-scoped file read/write
    path.py                # /{tenant_id}/{kb_id}/{doc_id}/{version}/...
  retrieval/
    __init__.py
    vectordb/
      client.py            # Qdrant client singleton
      collections.py       # Ensure collections per embedding profile
      tenant_filter.py     # Payload filter builder (tenant_id mandatory)
    search/
      fts.py               # PostgreSQL tsvector queries, ts_rank_cd
  control_plane/
    models/
      kb.py                # KnowledgeBase, Document, ChunkManifest (schema=app)
    repos/
      kb_repo.py
    services/
      kb_svc.py
      ingest/
        __init__.py
        pipeline.py        # Orchestrate: parse → chunk → enrich → embed → index
        parser.py           # PDF, DOCX, HTML, Markdown → structured text
        chunker.py          # Semantic splitting + parent-child hierarchy
        enricher.py         # Contextual chunking (Anthropic-style)
        embedder.py         # Batch embedding via model gateway
        indexer.py          # Dual write: Qdrant + PostgreSQL search tables
        layout.py           # Table and image extraction
apps/
  api-server/
    src/
      ragos_api/
        routes/
          tenant/
            kb.py
            documents.py
  ingest-worker/
    src/
      ragos_ingest_worker/
        __init__.py
        main.py            # Worker: consume ingest tasks from Valkey
```

### Key Interfaces

```python
# packages/control_plane/services/ingest/pipeline.py
class IngestPipeline:
    async def run(self, tenant_ctx: TenantContext, document_id: str) -> IngestResult:
        """Full pipeline: parse → chunk → enrich → embed → index."""
        ...

# packages/control_plane/services/ingest/chunker.py
@dataclass
class ChunkNode:
    chunk_id: str
    parent_chunk_id: str | None
    is_parent: bool
    text: str
    token_count: int
    metadata: dict

class SemanticChunker:
    def chunk(self, text: str, *, child_size: int = 400,
              parent_size: int = 1600) -> list[ChunkNode]: ...

# packages/control_plane/services/ingest/enricher.py
@dataclass
class EnrichedChunk:
    chunk: ChunkNode
    enriched_text: str   # "[Document context: ...]\n\n{chunk_text}" — for embedding
    display_text: str    # original text — for user display

class ContextualEnricher:
    async def enrich(self, doc_summary: str,
                     chunks: list[ChunkNode]) -> list[EnrichedChunk]: ...

# packages/control_plane/services/ingest/indexer.py
class DualIndexer:
    async def index(self, tenant_ctx: TenantContext, kb_id: str,
                    chunks: list[EnrichedChunk],
                    embeddings: list[list[float]]) -> IndexResult:
        """
        1. Write chunk_manifest rows to app schema (status=indexing)
        2. Upsert vectors + payload to Qdrant (idempotent)
        3. Upsert tsvector rows to search schema (idempotent)
        4. Mark chunk_manifest status=indexed
        On partial failure: leave status=indexing for retry.
        """
        ...

# packages/retrieval/vectordb/tenant_filter.py
def build_filter(tenant_id: str, *,
                 kb_ids: list[str] | None = None,
                 layers: list[str] | None = None,
                 is_parent: bool | None = None,
                 min_updated_at: int | None = None) -> models.Filter:
    """Build Qdrant payload filter. tenant_id is always required."""
    ...
```

### PostgreSQL Search Tables (search schema)

```sql
-- search.chunk_stable (L1 + L2 knowledge)
CREATE TABLE search.chunk_stable (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    layer TEXT NOT NULL,  -- L1, L2
    title TEXT,
    headings TEXT,
    body TEXT NOT NULL,
    tags TEXT[],
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(headings, '')), 'B') ||
        setweight(to_tsvector('simple', body), 'C') ||
        setweight(to_tsvector('simple', coalesce(array_to_string(tags, ' '), '')), 'D')
    ) STORED,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chunk_stable_search ON search.chunk_stable USING GIN (search_vector);
CREATE INDEX idx_chunk_stable_tenant ON search.chunk_stable (tenant_id, kb_id, layer, updated_at);

-- search.chunk_hot (L3 knowledge)
CREATE TABLE search.chunk_hot (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT[],
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', body) ||
        setweight(to_tsvector('simple', coalesce(array_to_string(tags, ' '), '')), 'B')
    ) STORED,
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chunk_hot_search ON search.chunk_hot USING GIN (search_vector);
CREATE INDEX idx_chunk_hot_tenant ON search.chunk_hot (tenant_id, kb_id, updated_at);

-- search.memory (memory facts for lexical recall)
CREATE TABLE search.memory (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', body)
    ) STORED,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_memory_search ON search.memory USING GIN (search_vector);
CREATE INDEX idx_memory_tenant ON search.memory (tenant_id, user_id);
```

### Qdrant Collection Setup

```python
await client.create_collection(
    collection_name="kb_dense_bge_m3",
    vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
)
for field in ["tenant_id", "kb_id", "layer", "lang", "is_parent", "security_level"]:
    await client.create_payload_index(
        "kb_dense_bge_m3", field_name=field, field_schema=PayloadSchemaType.KEYWORD)
for field in ["updated_at", "ttl_at"]:
    await client.create_payload_index(
        "kb_dense_bge_m3", field_name=field, field_schema=PayloadSchemaType.INTEGER)
```

### Acceptance Criteria

- [ ] Tenant admin creates KB (L1/L2/L3), uploads documents
- [ ] Ingest pipeline: parse → semantic chunk → contextual enrich → embed → dual index
- [ ] Parent-child chunks: children for retrieval, parents stored for context expansion
- [ ] Qdrant: vectors + payload with `parent_chunk_id`, `is_parent`, integer `updated_at`
- [ ] PostgreSQL FTS: `ts_rank_cd` returns tenant-scoped ranked results
- [ ] Indexing manifest: chunk_manifest tracks status (pending → indexing → indexed → failed)
- [ ] Partial failure: Qdrant success + FTS failure → status stays `indexing` for retry
- [ ] Ingest runs async: API → Valkey task → ingest-worker
- [ ] Document status: pending → parsing → chunking → embedding → indexed → failed
- [ ] Tests: parser, chunker (parent-child tree), enricher (mock LLM), indexer (PG + Qdrant)

---

## M4: RAG Pipeline

**Goal:** Full retrieval chain: query engine → multi-route recall → fusion → rerank → CRAG gate → generation with citations.

### Deliverables

```text
packages/
  retrieval/
    pipeline.py            # Full retrieval chain orchestrator
    query/
      engine.py            # Query strategy selector (direct, HyDE, decompose)
      rewriter.py          # Multi-turn context-aware rewrite
      hyde.py              # Hypothetical Document Embedding
      decomposer.py        # Query decomposition
    recall/
      bm25.py              # PostgreSQL FTS recall (ts_rank_cd)
      dense.py             # Qdrant dense vector recall
      memory.py            # Memory recall (stub, completed in M5)
      hot.py               # L3 hot-tier recall with freshness priority
      web.py               # Web search recall (optional, stub)
    fusion.py              # RRF, weighted, layer/freshness/authority boost
    reranker.py            # Cross-encoder or LLM reranker via model gateway
    grader.py              # CRAG relevance gate
    citation.py            # Citation extraction and linking
    expander.py            # Child → parent chunk expansion
    types.py               # RetrievalResult, Citation, GradeResult, FusionConfig, etc.
  control_plane/
    models/
      retriever_profile.py
    repos/
      retriever_profile_repo.py
```

### Key Interfaces

```python
# packages/retrieval/pipeline.py
class RAGPipeline:
    async def run(self, query: str, tenant_ctx: TenantContext,
                  profile: RetrieverProfile,
                  conversation_history: list[Message] | None = None
                  ) -> RAGResult: ...

@dataclass
class RAGResult:
    answer: str
    citations: list[Citation]
    retrieval_explain: RetrievalExplain
    tokens_used: TokenUsage
    grade: GradeResult

# packages/retrieval/grader.py
@dataclass
class GradeResult:
    verdict: Literal["relevant", "ambiguous", "irrelevant"]
    confidence: float
    action: Literal["proceed", "re_retrieve", "refuse"]
    rewritten_query: str | None

class RelevanceGrader:
    async def grade(self, query: str, chunks: list[ScoredChunk],
                    tenant_ctx: TenantContext) -> GradeResult: ...

# packages/retrieval/fusion.py
@dataclass
class FusionConfig:
    method: Literal["rrf", "weighted"]
    layer_boost: dict[str, float]    # {"L3": 1.5, "L2": 1.0, "L1": 0.8}
    freshness_decay: float
    authority_boost: float
    rrf_k: int = 60

class Fusioner:
    def fuse(self, channels: dict[str, list[ScoredChunk]],
             config: FusionConfig) -> list[ScoredChunk]: ...

# packages/retrieval/expander.py
class ChunkExpander:
    async def expand_to_parents(self, chunks: list[ScoredChunk],
                                tenant_ctx: TenantContext) -> list[ScoredChunk]:
        """Fetch parent chunks from Qdrant, deduplicate overlapping parents."""
        ...

# packages/retrieval/types.py
@dataclass
class RetrieverProfile:
    id: str
    tenant_id: str
    query_strategy: Literal["direct", "hyde", "decompose"]
    recall_channels: list[str]
    fusion_config: FusionConfig
    rerank_model_profile: str | None
    crag_enabled: bool
    top_k_recall: int = 50
    top_k_rerank: int = 20
    top_k_generate: int = 6

# packages/retrieval/recall/bm25.py
class BM25Recall:
    async def recall(self, query: str, tenant_ctx: TenantContext,
                     kb_ids: list[str] | None, top_k: int
                     ) -> list[ScoredChunk]:
        """
        SELECT *, ts_rank_cd(search_vector, websearch_to_tsquery('simple', :query)) AS rank
        FROM search.chunk_stable
        WHERE tenant_id = :tenant_id AND search_vector @@ websearch_to_tsquery(...)
        ORDER BY rank DESC LIMIT :top_k
        """
        ...
```

### Acceptance Criteria

- [ ] Query rewrite produces context-aware queries from multi-turn conversations
- [ ] HyDE generates hypothetical answer embedding for vague queries
- [ ] BM25 recall: `ts_rank_cd` on `search.chunk_stable`, scoped to tenant
- [ ] Dense recall: Qdrant search with payload filter, scoped to tenant
- [ ] Fusion: configurable RRF/weighted/boost per retriever profile
- [ ] Reranker re-scores top-N via model gateway
- [ ] CRAG gate: grades quality, re-retrieves on "ambiguous", refuses on "irrelevant"
- [ ] Child chunks expanded to parents before generation
- [ ] Citations: structured objects with doc/chunk references
- [ ] RetrievalExplain captures full pipeline trace
- [ ] Tests: each stage independently, end-to-end with mock LLM

---

## M5: Chat & Memory

**Goal:** Conversation API, SSE streaming, three-tier memory with fact extraction and rolling summarization.

### Deliverables

```text
packages/
  memory/
    __init__.py
    manager.py             # Three-tier memory orchestrator
    fact_store.py          # PostgreSQL: facts CRUD with decay scoring
    vector_store.py        # Qdrant: memory vector search
    extractor.py           # LLM fact extraction from conversations
    summarizer.py          # Rolling conversation summarization
  control_plane/
    models/
      conversation.py      # Conversation, Message (schema=app)
      memory.py            # MemoryFact, MemorySummary (schema=app)
    repos/
      conversation_repo.py
      memory_repo.py
    services/
      chat_svc.py          # Chat orchestration entry point
apps/
  api-server/
    src/
      ragos_api/
        routes/
          chat/
            conversations.py
            messages.py    # POST message → SSE stream
            feedback.py
```

### Key Interfaces

```python
# packages/memory/manager.py
@dataclass
class MemoryContext:
    system_facts: list[MemoryFact]       # relevant long-term facts
    conversation_summary: str | None     # compressed older turns
    recent_messages: list[Message]       # verbatim recent N turns

class MemoryManager:
    async def build_context(self, tenant_ctx: TenantContext,
                            user_id: str,
                            conversation_id: str) -> MemoryContext: ...

    async def update_after_conversation(self, tenant_ctx: TenantContext,
                                        user_id: str,
                                        conversation_id: str,
                                        messages: list[Message]) -> None:
        """Extract facts + update rolling summary.
        Runs as a durable async task — NOT fire-and-forget:
        1. Insert task row into app.task (type='memory_writeback',
           idempotency_key='{conversation_id}:{message_count}',
           status='pending', payload=serialized args)
        2. NOTIFY via Valkey to wake workflow-worker
        3. Worker picks up task, calls _do_writeback(), sets status='completed'
        4. On failure: retry with backoff, max 3 attempts, then status='failed'
        Idempotency: skip if a completed task with same idempotency_key exists.
        """
        ...

# packages/control_plane/services/chat_svc.py
class ChatService:
    async def send_message(self, tenant_ctx: TenantContext,
                           conversation_id: str,
                           user_message: str) -> AsyncIterator[SSEEvent]:
        """
        1. Load memory context (facts + summary + recent turns)
        2. Build RAG prompt with memory as separate context section
        3. Run RAG pipeline (retrieve → fuse → rerank → grade → generate)
        4. Stream token deltas as SSE events
        5. Save message + citations to PostgreSQL
        6. Dispatch async memory update (fact extraction + summarization)
        """
        ...
```

### SSE Event Protocol

```text
POST /chat/conversations/{conv_id}/messages
Content-Type: application/json
Accept: text/event-stream

Response stream:

id: 1
event: run_started
data: {"run_id":"run_abc","ts":1712700000}

id: 2
event: retrieval_completed
data: {"channels":4,"total_chunks":42}

id: 3
event: rerank_completed
data: {"top_k":6}

id: 4
event: relevance_graded
data: {"verdict":"relevant","confidence":0.92}

id: 5
event: token_delta
data: {"delta":"根据"}

id: 6
event: citation_linked
data: {"citations":[{"doc_id":"doc_1","chunk_id":"chk_5","text":"..."}]}

id: 7
event: memory_updated
data: {"facts_extracted":2,"summary_updated":true}

id: 8
event: run_finished
data: {"run_id":"run_abc","tokens":1520,"latency_ms":3200}
```

### Memory DB Models

```sql
-- app.memory_fact
CREATE TABLE app.memory_fact (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    category TEXT NOT NULL,  -- preference, behavior, context, entity
    confidence REAL NOT NULL DEFAULT 1.0,
    decay_score REAL NOT NULL DEFAULT 1.0,
    source_conversation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memory_fact_tenant ON app.memory_fact (tenant_id, user_id, decay_score DESC);

-- app.memory_summary
CREATE TABLE app.memory_summary (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES app.conversation(id),
    summary TEXT NOT NULL,
    turn_range INT4RANGE NOT NULL,  -- [start_turn, end_turn)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Acceptance Criteria

- [ ] `POST /chat/conversations` creates conversation
- [ ] `POST /chat/conversations/{id}/messages` returns SSE with typed events
- [ ] Client receives: run_started, retrieval_completed, relevance_graded, token_delta, citation_linked, run_finished
- [ ] SSE reconnect via `Last-Event-ID` works
- [ ] Memory context: facts + summary injected as separate section in RAG prompt
- [ ] Fact extraction runs asynchronously after conversation
- [ ] Rolling summarization compresses older turns
- [ ] `decay_score` decreases over time, resets on access
- [ ] Feedback endpoint stores thumbs up/down
- [ ] Tests: SSE stream, memory context, fact extraction (mock LLM)

---

## M6: Workflow Engine

**Goal:** DSL-driven workflow engine: compiler, LangGraph runtime, checkpointing in PostgreSQL, human-in-the-loop, SSE event stream.

### Deliverables

```text
packages/
  runtime/
    __init__.py
    dsl.py                 # Pydantic DSL schema
    compiler.py            # DSL → LangGraph StateGraph
    executor.py            # Graph execution, checkpointing, resume
    state.py               # WorkflowState TypedDict
    events.py              # Event types
    nodes/
      registry.py          # Node type → handler mapping
      llm.py
      retrieve.py
      condition.py
      loop.py
      subflow.py
  control_plane/
    models/
      workflow.py          # WorkflowDef, WorkflowVersion (schema=app)
      prompt.py            # PromptDef, PromptVersion
      run.py               # Run, RunSummary
    repos/
      workflow_repo.py
      prompt_repo.py
      run_repo.py
    services/
      workflow_svc.py
      prompt_svc.py
apps/
  api-server/
    src/
      ragos_api/
        routes/
          tenant/
            workflows.py
            prompts.py
  workflow-worker/
    src/
      ragos_workflow_worker/
        __init__.py
        main.py
```

### Key Interfaces

```python
# packages/runtime/dsl.py
class NodeDef(BaseModel):
    id: str
    type: Literal["llm", "retrieve", "condition", "loop", "http",
                   "script", "mcp_tool", "notify", "subflow"]
    config: dict               # node-specific config (stored as JSONB)
    retry: RetryConfig | None = None
    timeout_seconds: int = 60

class EdgeDef(BaseModel):
    from_node: str
    to_node: str
    condition: str | None = None

class WorkflowDSL(BaseModel):
    nodes: list[NodeDef]
    edges: list[EdgeDef]
    input_schema: dict | None = None
    output_schema: dict | None = None
    interrupt_before: list[str] = []
    timeout_seconds: int = 300

# packages/runtime/compiler.py
class WorkflowCompiler:
    def __init__(self, node_registry: NodeRegistry): ...
    def compile(self, dsl: WorkflowDSL) -> CompiledStateGraph:
        """Pure function. Cached by workflow_version hash.
        tenant_ctx is NOT a compile parameter — it is injected at runtime
        via config['configurable'] to keep compiled graphs tenant-agnostic
        and safely cacheable across tenants."""
        ...

# packages/runtime/executor.py
class WorkflowExecutor:
    def __init__(self, checkpointer: AsyncPostgresSaver): ...

    async def run(self, graph: CompiledStateGraph, input: dict,
                  tenant_ctx: TenantContext, run_id: str
                  ) -> AsyncIterator[WorkflowEvent]:
        """
        thread_id = f"{tenant_ctx.tenant_id}:{workflow_id}:{run_id}"
        async for event in graph.astream_events(input, config, version="v2"):
            yield map_to_workflow_event(event)
        """
        ...

    async def resume(self, run_id: str, tenant_ctx: TenantContext,
                     user_input: dict) -> AsyncIterator[WorkflowEvent]:
        """Resume via Command(resume=value) with same thread_id."""
        ...

# packages/runtime/state.py
class WorkflowState(TypedDict):
    tenant_ctx: dict
    run_id: str
    user_input: str
    messages: list[dict]
    variables: dict
    retrieved_docs: list[dict]
    tool_results: list[dict]
    citations: list[dict]
    errors: list[dict]

# packages/runtime/nodes/registry.py
class NodeRegistry:
    _handlers: dict[str, Callable]

    def register(self, node_type: str, handler: Callable) -> None: ...
    def build_node(self, node_def: NodeDef) -> Callable:
        """Return partial(handler, config=node_def.config)."""
        ...
```

### LangGraph Checkpointer Setup

```python
# packages/runtime/checkpoints.py
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def langgraph_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    checkpoint_dsn = build_checkpoint_dsn(settings.pg_dsn, settings.pg_checkpoint_schema)
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
```

### Acceptance Criteria

- [ ] DSL validates with Pydantic; invalid DSL → clear error
- [ ] Compiler produces runnable LangGraph from DSL
- [ ] Execution yields typed events via async iterator
- [ ] Checkpointing in PostgreSQL `checkpoint` schema; process restart recovers
- [ ] HITL: interrupt at designated nodes, resume with user input
- [ ] LLM and Retrieve nodes work end-to-end
- [ ] Condition evaluates expressions; Loop iterates; Subflow invokes subgraphs
- [ ] Run summary written to `app.run_summary`
- [ ] `RetryPolicy` applied per node for transient errors
- [ ] Tests: compiler, executor (mock nodes), resume from checkpoint

---

## M7: Tool Nodes & Security

**Goal:** HTTP, Script, MCP, Notify nodes with full security controls.

### Deliverables

```text
packages/
  toolhub/
    __init__.py
    http_client.py         # Domain whitelist, SSRF, timeout, secret injection, audit
    mcp_client.py          # MCP client wrapper
    notify.py              # Webhook / email dispatcher
    secret_resolver.py     # Resolve aliases to decrypted values, tenant-scoped
  sandbox/
    __init__.py
    client.py              # RPC client — dispatches to sandbox-runner via Valkey
    policy.py              # Import whitelist, network policy
  runtime/
    nodes/
      http.py
      script.py            # Delegates to SandboxClient (RPC, not local subprocess)
      mcp_tool.py
      notify.py
  control_plane/
    models/
      secret.py            # TenantSecret (encrypted at rest)
    repos/
      secret_repo.py
apps/
  sandbox-runner/
    src/
      ragos_sandbox_runner/
        __init__.py
        main.py            # Separate process: dequeue tasks, subprocess execute, return results
        executor.py        # Actual subprocess sandbox with resource limits (runs here only)
```

### Key Interfaces

```python
# packages/toolhub/http_client.py
class SecuredHttpClient:
    async def request(self, method: str, url: str, *,
                      tenant_ctx: TenantContext,
                      secret_refs: dict[str, str] | None = None,
                      timeout: float = 30,
                      idempotency_key: str | None = None) -> HttpResult:
        """Pre: domain whitelist, SSRF block. Post: audit log."""
        ...

# packages/sandbox/client.py
class SandboxClient:
    """RPC client that dispatches script execution to sandbox-runner via Valkey task queue.
    Never runs user code in the API or worker process."""

    async def run_script(self, code: str, *, input_data: dict,
                         tenant_ctx: TenantContext,
                         timeout_seconds: int = 10,
                         memory_limit_mb: int = 128,
                         allow_imports: list[str] | None = None,
                         idempotency_key: str | None = None) -> ScriptResult:
        """
        1. Enqueue task to Valkey (queue: sandbox:{tenant_id})
        2. Await result with timeout (Valkey BRPOP on result key)
        3. Return ScriptResult or raise SandboxError / TimeoutError
        Phase 1 stub: raise SandboxError("sandbox-runner not available")
        """
        ...

# apps/sandbox-runner/src/ragos_sandbox_runner/main.py — sandbox-runner process
class SandboxRunner:
    """Dequeues script tasks from Valkey, executes in subprocess with
    resource limits (CPU, memory, timeout, no network, import whitelist),
    publishes result back to Valkey result key."""
    async def run_forever(self) -> None: ...
```

### Acceptance Criteria

- [ ] HTTP node: domain whitelist, private IP blocked, secrets injected by alias, audit logged
- [ ] Script node: SandboxClient dispatches via Valkey RPC to sandbox-runner process
- [ ] sandbox-runner: subprocess with CPU/memory/timeout limits, import whitelist, no network
- [ ] MCP node: connects to server, invokes tools
- [ ] Phase 1: SandboxClient raises SandboxError when sandbox-runner is not running
- [ ] Secrets: encrypted at rest, resolved by alias, never in logs
- [ ] Tests: SSRF blocking, sandbox limits, secret resolution

---

## M8: Observability & Eval

**Goal:** Retrieval explain, workflow trace, RAGAS evaluation, feedback loop.

### Deliverables

```text
packages/
  observability/
    __init__.py
    tracing.py             # Structured logging + OTel span helpers
    retrieval_explain.py   # Build explain data from RAGResult
    workflow_trace.py      # Build trace from run events
  control_plane/
    models/
      eval.py              # EvalCase, EvalRun, FeedbackEntry (schema=app)
    repos/
      eval_repo.py
    services/
      eval_svc.py          # RAGAS metrics, bundle management
      feedback_svc.py
apps/
  api-server/
    src/
      ragos_api/
        routes/
          tenant/
            analytics.py
            eval.py
  scheduler-worker/
    src/
      ragos_scheduler_worker/
        __init__.py
        main.py            # Cron: periodic eval, TTL cleanup, memory decay
```

### Key Interfaces

```python
# packages/control_plane/services/eval_svc.py
@dataclass
class EvalBundle:
    """Frozen version snapshots — not mutable profile IDs.
    Ensures eval results are reproducible even if profiles change later."""
    prompt_version_hash: str          # content hash of the prompt template
    workflow_version_hash: str        # content hash of the DSL JSON
    model_snapshot: dict              # {provider, model_name, temperature, ...}
    retriever_snapshot: dict          # {top_k, reranker, fusion_weights, ...}
    parser_version: str               # parser code version tag
    chunker_version: str              # chunker code version tag
    embedding_model: str              # exact embedding model used

@dataclass
class RAGASMetrics:
    faithfulness: float
    answer_relevancy: float
    context_precision: float
    context_recall: float

class EvalService:
    async def run_eval(self, tenant_ctx: TenantContext, bundle: EvalBundle,
                       test_cases: list[EvalCase]) -> EvalRunResult: ...
    async def compare_bundles(self, tenant_ctx: TenantContext,
                              bundle_a: str, bundle_b: str) -> BundleComparison: ...
```

### Acceptance Criteria

- [ ] Retrieval explain: rewritten query, per-channel recall, fusion scores, CRAG grade, citations
- [ ] Workflow trace: graph path, node I/O, tool calls, retries, errors, latency
- [ ] RAGAS eval: faithfulness, relevancy, precision, recall on test cases
- [ ] Bundle comparison: side-by-side metric diff
- [ ] Feedback stored, writable back to eval dataset
- [ ] Scheduler: memory decay, L3 TTL cleanup, periodic eval
- [ ] Tests: RAGAS computation (mocked), explain builder

---

## M9: Frontend

**Goal:** Three-segment frontend: platform admin, tenant admin, branded chat UI.

### Deliverables

```text
apps/
  web/
    platform/              # Tenant management, model center, global eval
    tenant/                # KB management, workflow editor, brand config, analytics
    chat/                  # Branded chat with SSE, citations, feedback
    shared/
      api-client.js        # Fetch wrapper with auth
      sse.js               # EventSource with typed events + reconnect
```

### Acceptance Criteria

- [ ] Platform admin: tenant CRUD, model center, global eval
- [ ] Tenant admin: KB, documents, workflows, prompts, brand, analytics, eval
- [ ] Chat UI: branded (theme/logo/welcome), SSE streaming, citations, feedback
- [ ] Workflow trace viewer, retrieval explain panel
- [ ] SSE reconnect with `Last-Event-ID`
- [ ] RBAC enforced across all pages

---

## M10: Hardening & Ship

**Goal:** Security audit, rate limiting, performance, deployment packaging.

### Deliverables

```text
infra/
  docker/
    Dockerfile
    docker-compose.prod.yml
  scripts/
    seed.py                # Demo tenant with sample data
    reindex.py             # Full reindex utility
```

### Checklist

- [ ] **Rate limiting**:
  - Token bucket per tenant via Valkey, 429 + Retry-After header
  - Per-endpoint-class limits: `/chat/*` (strict), `/tenant/*` (moderate), `/platform/*` (relaxed)
  - `plan_tier` differentiation: free/starter/business/enterprise with different rate ceilings
  - LLM token quota: per-tenant daily/monthly token budget tracked in PostgreSQL, soft limit warning + hard limit block
  - Concurrent request limit per tenant (semaphore via Valkey)
- [ ] **SSRF audit**: all outbound HTTP verified against private IP block
- [ ] **Secret audit**: no secrets in logs, API responses, errors
- [ ] **SQL injection audit**: all queries via SQLAlchemy ORM
- [ ] **XSS audit**: user content sanitized
- [ ] **Tenant isolation audit**: automated test — tenant A cannot see tenant B's data (app + search + Qdrant)
- [ ] **RLS audit**: RLS policies active on all tenant-scoped tables
- [ ] **Error handling audit**: all domain exceptions mapped, no stack traces leaked
- [ ] **Performance**: chat latency p50/p95/p99, ingest throughput
- [ ] **PgBouncer**: connection pooler deployed in production
- [ ] **Autovacuum**: tuned for hot write tables (`task`, `run`, `search.chunk_hot`)
- [ ] **Backup**: `pg_dump` schedule, Qdrant snapshots, file storage rsync
- [ ] **Deployment guide**: Docker Compose with all 5 process roles
- [ ] **Demo seed**: one tenant with sample KB, conversations, workflow

---

## Cross-Cutting: Testing Strategy

| Layer | Scope | Database | External Services | Speed |
|-------|-------|----------|-------------------|-------|
| Unit | Single function/class | None or mock | All mocked | Fast (<1s) |
| Integration | Service + repo + DB | Test PostgreSQL | LLM mocked | Medium (<10s) |
| Eval | RAG pipeline E2E | Real DB + Qdrant | Real LLM (or cached) | Slow (<60s) |

### Test Fixtures (`tests/conftest.py`, when added)

```python
@pytest.fixture
async def test_db(tmp_path) -> AsyncSession:
    """Ephemeral PostgreSQL test database with migrations applied."""
    # Use testcontainers-python or a dedicated test DB
    ...

@pytest.fixture
def test_tenant_ctx() -> TenantContext:
    """Standard test tenant context."""
    return TenantContext(
        tenant_id="t_test", tenant_slug="test", brand_id=None,
        actor_id="u_test", actor_type="user", roles=frozenset({"tenant_admin"}),
        plan_tier="pro", allowed_kb_ids=None, model_binding_id=None,
        locale="zh-CN", timezone="Asia/Shanghai",
        trace_id=str(uuid4()),
    )

@pytest.fixture
async def test_qdrant() -> QdrantClient:
    """Qdrant client pointing to test collection (cleaned between tests)."""
    ...
```

### What to Test

- **Tenant isolation**: every repo test verifies tenant A cannot see tenant B's data
- **RLS**: verify RLS policies block cross-tenant access even without repo scoping
- **Pipeline stages**: chunk, enrich, embed, fuse, rerank, grade — independently
- **Workflow compiler**: DSL → graph correctness, invalid DSL rejection
- **SSE protocol**: event sequence, reconnect
- **Security**: SSRF blocking, sandbox limits, secret resolution
- **Eval**: RAGAS metrics on known test cases

---

## Cross-Cutting: Dependencies

```toml
[project]
name = "ragos"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "psycopg[binary]>=3.2",
    "alembic>=1.13",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "langgraph>=0.2",
    "langgraph-checkpoint-postgres>=2.0",
    "langchain-core>=0.3",
    "qdrant-client>=1.12",
    "httpx>=0.27",
    "pyjwt>=2.9",
    "redis>=5.0",
    "sse-starlette>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "testcontainers[postgres]>=4.0",
    "ruff>=0.5",
    "mypy>=1.10",
]
```

---

## Cross-Cutting: Configuration

```bash
# .env.development
DATABASE_URL=postgresql+asyncpg://ragos:ragos_dev@localhost:5432/ragos
DATABASE_URL_SYNC=postgresql+psycopg://ragos:ragos_dev@localhost:5432/ragos
STORAGE_ROOT=data/storage
QDRANT_URL=http://localhost:6333
VALKEY_URL=redis://localhost:6379
JWT_SECRET=dev-secret-change-me
DEBUG=true

# .env.production (example)
DATABASE_URL=postgresql+asyncpg://ragos:${DB_PASSWORD}@postgres:5432/ragos
DATABASE_URL_SYNC=postgresql+psycopg://ragos:${DB_PASSWORD}@pgbouncer:6432/ragos
STORAGE_ROOT=/var/lib/ragos/storage
QDRANT_URL=http://qdrant:6333
VALKEY_URL=redis://valkey:6379
JWT_SECRET=${JWT_SECRET}
DEBUG=false
```
