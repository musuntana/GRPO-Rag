from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from ragos.core.config import get_settings
from ragos.core.logging import configure_logging
from ragos.runtime.checkpoints import langgraph_checkpointer
from ragos.storage.db import ensure_schemas, get_engine, require_migrated_schema
from ragos.tenant.middleware import RequestContextMiddleware

from ragos_api.errors import register_error_handlers
from ragos_api.routes.chat import router as chat_router
from ragos_api.routes.kb import router as kb_router
from ragos_api.routes.model_bindings import router as model_bindings_router
from ragos_api.routes.model_center import router as model_center_router
from ragos_api.routes.platform import router as platform_router
from ragos_api.routes.platform_users import router as platform_users_router
from ragos_api.routes.retrieval import router as retrieval_router
from ragos_api.routes.retriever_profiles import router as retriever_profiles_router
from ragos_api.routes.search import router as search_router
from ragos_api.routes.tasks import router as tasks_router
from ragos_api.routes.tenant import router as tenant_router
from ragos_api.routes.users import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    configure_logging("INFO")
    if settings.app_env not in {"dev", "test"}:
        raise RuntimeError(
            "production startup unavailable: signed authentication "
            "and membership authorization are not connected"
        )

    if app.state.testing:
        yield
        return

    engine = get_engine(settings)
    require_migrated_schema(engine, settings)
    ensure_schemas(engine, settings)

    async with langgraph_checkpointer(settings) as checkpointer:
        app.state.engine = engine
        app.state.langgraph_checkpointer = checkpointer
        yield


def create_app(*, testing: bool = False) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="RAGOS API", lifespan=lifespan)
    app.state.settings = settings
    app.state.testing = testing
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(chat_router)
    app.include_router(retrieval_router)
    app.include_router(retriever_profiles_router)
    app.include_router(tenant_router)
    app.include_router(tasks_router)
    app.include_router(kb_router)
    app.include_router(search_router)
    app.include_router(platform_router)
    app.include_router(platform_users_router)
    app.include_router(users_router)
    app.include_router(model_center_router)
    app.include_router(model_bindings_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        cfg = app.state.settings
        return {
            "status": "ok",
            "app_schema": cfg.pg_app_schema,
            "search_schema": cfg.pg_search_schema,
            "checkpoint_schema": cfg.pg_checkpoint_schema,
        }

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "ragos_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env == "dev",
    )
