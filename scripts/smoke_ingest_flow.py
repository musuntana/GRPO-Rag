#!/usr/bin/env python3
"""End-to-end smoke test for the ingest_document task pipeline.

Exercises:
    api-server  → POST /tenant/kb                          (create KB)
                → POST /tenant/kb/{kb_id}/documents        (register document)
                → POST /tenant/kb/{kb_id}/documents/{id}/ingest
                                                           (create manifest + enqueue task)
    postgres    → chunk_manifest row transitions pending → indexed
                → search.chunk_stable rows inserted
    worker      → ingest_document handler via IngestionService

Prerequisites:
    1. docker-compose services up    (make up)
    2. schema migrated               (make migrate)
    3. api-server running            (make api)
    4. workflow-worker running       (make worker)

Usage:
    python scripts/smoke_ingest_flow.py [--timeout 15]

Notes:
    - When RAGOS_QDRANT_URL is empty the worker uses NoopVectorIndexWriter, so
      this script can validate the PG dual-write path without Qdrant running.
    - Safe to re-run — the tenant and KB are looked up by slug before insert.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from uuid import UUID, uuid4

import httpx
from ragos.auth.jwt import encode_dev_token
from ragos.core.config import Settings, get_settings
from ragos.storage.db import get_session_factory
from ragos.storage.models.app import ChunkManifest, KnowledgeBase, Task, Tenant
from ragos.storage.models.search import SearchChunkStable
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, select

SEED_TENANT_SLUG = "smoke-ingest-tenant"
SEED_TENANT_DISPLAY = "Smoke Ingest Tenant"
SEED_KB_SLUG = "smoke-kb"


def ensure_seed_tenant(session_factory: sessionmaker[Session]) -> Tenant:
    with session_factory() as session:
        with session.begin():
            existing = session.exec(
                select(Tenant).where(Tenant.slug == SEED_TENANT_SLUG)
            ).first()
            if existing is not None:
                session.expunge(existing)
                return existing
            tenant = Tenant(slug=SEED_TENANT_SLUG, display_name=SEED_TENANT_DISPLAY)
            session.add(tenant)
            session.flush()
            session.refresh(tenant)
            session.expunge(tenant)
            return tenant


def ensure_seed_kb(
    session_factory: sessionmaker[Session],
    tenant_id: UUID,
    base_url: str,
    headers: dict[str, str],
) -> dict:
    """Return an existing KB by slug or create a fresh one through the API."""
    with session_factory() as session:
        existing = session.exec(
            select(KnowledgeBase)
            .where(KnowledgeBase.tenant_id == tenant_id)
            .where(KnowledgeBase.slug == SEED_KB_SLUG)
        ).first()
        if existing is not None:
            return {
                "id": str(existing.id),
                "slug": existing.slug,
                "name": existing.name,
            }

    resp = httpx.post(
        f"{base_url}/tenant/kb",
        headers=headers,
        json={
            "slug": SEED_KB_SLUG,
            "name": "Smoke KB",
            "description": "Used by scripts/smoke_ingest_flow.py",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def make_dev_token(tenant: Tenant) -> str:
    return encode_dev_token(
        {
            "sub": "smoke-ingest-script",
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "actor_type": "tenant_admin",
            "roles": ["tenant_admin"],
        }
    )


def create_document(base_url: str, kb_id: str, headers: dict[str, str]) -> dict:
    resp = httpx.post(
        f"{base_url}/tenant/kb/{kb_id}/documents",
        headers=headers,
        json={"title": f"Smoke Document {uuid4()}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def trigger_ingest(
    base_url: str,
    kb_id: str,
    doc_id: str,
    headers: dict[str, str],
) -> dict:
    resp = httpx.post(
        f"{base_url}/tenant/kb/{kb_id}/documents/{doc_id}/ingest",
        headers=headers,
        json={
            "tier": "stable",
            "idempotency_key": f"smoke-ingest-{uuid4()}",
            "chunks": [
                {
                    "body": "RagOS is a multi-tenant RAG SaaS platform.",
                    "title": "Intro",
                    "layer": "L1",
                    "metadata_json": {"source": "smoke"},
                },
                {
                    "body": "Tenant isolation is enforced via tenant_ctx and "
                    "Qdrant payload filters.",
                    "layer": "L1",
                    "metadata_json": {"source": "smoke"},
                },
            ],
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_task_completion(
    session_factory: sessionmaker[Session],
    task_id: UUID,
    *,
    timeout: float,
) -> Task:
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        with session_factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise SystemExit(f"task {task_id} disappeared from app.task")
            last_status = task.status
            if task.status == "completed":
                session.expunge(task)
                return task
            if task.status == "failed":
                raise SystemExit(
                    f"task {task_id} reached terminal status 'failed' "
                    f"(attempts={task.attempts})"
                )
        time.sleep(0.2)
    raise SystemExit(
        f"task {task_id} did not complete within {timeout:.0f}s "
        f"(last status: {last_status!r})"
    )


def fetch_manifest_state(
    session_factory: sessionmaker[Session],
    manifest_id: UUID,
) -> ChunkManifest:
    with session_factory() as session:
        manifest = session.get(ChunkManifest, manifest_id)
        if manifest is None:
            raise SystemExit(f"manifest {manifest_id} disappeared from app.chunk_manifest")
        session.expunge(manifest)
        return manifest


def count_stable_chunks(
    session_factory: sessionmaker[Session],
    tenant_id: UUID,
    document_id: UUID,
) -> int:
    with session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(SearchChunkStable)
            .where(SearchChunkStable.tenant_id == tenant_id)
            .where(SearchChunkStable.document_id == document_id)
        )
        return int(session.exec(stmt).one())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the ingest task to reach 'completed'",
    )
    args = parser.parse_args()

    settings: Settings = get_settings()
    session_factory = get_session_factory(settings)

    host = "127.0.0.1" if settings.api_host in ("0.0.0.0", "") else settings.api_host
    base_url = f"http://{host}:{settings.api_port}"

    print(f"[1/6] seeding tenant slug={SEED_TENANT_SLUG!r}")
    tenant = ensure_seed_tenant(session_factory)
    print(f"      tenant_id={tenant.id}")

    print("[2/6] encoding dev bearer token (role=tenant_admin)")
    token = make_dev_token(tenant)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(tenant.id),
    }

    print(f"[3/6] ensuring KB slug={SEED_KB_SLUG!r}")
    kb = ensure_seed_kb(session_factory, tenant.id, base_url, headers)
    kb_id = kb["id"]
    print(f"      kb_id={kb_id}")

    print("[4/6] creating document")
    doc = create_document(base_url, kb_id, headers)
    doc_id = doc["id"]
    print(f"      document_id={doc_id}")

    print("[5/6] triggering ingest_document")
    ingest_resp = trigger_ingest(base_url, kb_id, doc_id, headers)
    task_id = UUID(ingest_resp["task_id"])
    manifest_id = UUID(ingest_resp["manifest"]["id"])
    print(f"      task_id={task_id} manifest_id={manifest_id}")

    print(f"[6/6] waiting for task completion (timeout={args.timeout:.0f}s)")
    task = wait_for_task_completion(session_factory, task_id, timeout=args.timeout)

    manifest = fetch_manifest_state(session_factory, manifest_id)
    stable_count = count_stable_chunks(session_factory, tenant.id, UUID(doc_id))

    if manifest.indexing_state != "indexed":
        raise SystemExit(
            f"manifest {manifest_id} expected indexing_state='indexed', "
            f"got {manifest.indexing_state!r} (error_message={manifest.error_message!r})"
        )
    if stable_count != 2:
        raise SystemExit(
            f"expected 2 rows in search.chunk_stable for document {doc_id}, got {stable_count}"
        )

    print("      SUCCESS")
    print(
        json.dumps(
            {
                "task_id": str(task.id),
                "task_status": task.status,
                "manifest_id": str(manifest.id),
                "manifest_state": manifest.indexing_state,
                "manifest_chunk_total": manifest.chunk_total,
                "chunk_stable_rows": stable_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"HTTP {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
        sys.exit(2)
    except httpx.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        sys.exit(2)
