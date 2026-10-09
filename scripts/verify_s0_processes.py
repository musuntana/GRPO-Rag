"""Verify installed imports and truthful process capabilities.

Run only against a disposable migrated PostgreSQL database configured through
RAGOS_PG_DSN. The API check initializes LangGraph checkpoint tables.
"""

import asyncio
import importlib
import pkgutil

import ragos.auth
import ragos.core
import ragos.memory
import ragos.model_gateway
import ragos.retrieval
import ragos.runtime
import ragos.storage
import ragos.tenant
from fastapi.testclient import TestClient
from ragos_api.main import create_app

count = 0
for package in (
    ragos.core,
    ragos.auth,
    ragos.tenant,
    ragos.storage,
    ragos.runtime,
    ragos.memory,
    ragos.retrieval,
    ragos.model_gateway,
):
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(info.name)
        count += 1
print("installed modules imported:", count)
for retired in ("ragos.runtime.vector", "ragos.runtime.chunking"):
    assert importlib.util.find_spec(retired) is None, f"retired module still installed: {retired}"
assert importlib.util.find_spec("ragos.storage.vector") is not None
for name in ("ragos_workflow_worker", "ragos_ingest_worker", "ragos_train_worker"):
    main = importlib.import_module(name + ".main")
    try:
        asyncio.run(main.serve())
    except (RuntimeError, NotImplementedError) as exc:
        print(name, "explicitly unavailable:", str(exc))
    else:
        raise AssertionError("unimplemented worker unexpectedly started")
with TestClient(create_app()) as client:
    assert client.get("/healthz").status_code == 200
    assert client.post("/chat/conversations", json={}).status_code == 401
    print("API actual lifespan/checkpoint setup, healthz and missing-auth rejection passed")
