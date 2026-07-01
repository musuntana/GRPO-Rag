#!/usr/bin/env python3
"""End-to-end smoke test for the task worker pipeline.

Exercises:
    api-server  → POST /tenant/tasks (enqueue)
    dev bearer  → auth + role check
    postgres    → app.task row insert + idempotency
    worker      → claim → dispatch → log_only_handler → mark completed

Prerequisites:
    1. docker-compose services up    (make up)
    2. schema migrated               (make migrate)
    3. api-server running            (make api)
    4. workflow-worker running       (make worker)

Usage:
    python scripts/smoke_task_worker.py [--timeout 15]
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
from ragos.storage.models.app import Task, Tenant
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, select

SEED_TENANT_SLUG = "smoke-tenant"
SEED_TENANT_DISPLAY = "Smoke Tenant"


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


def make_dev_token(tenant: Tenant) -> str:
    return encode_dev_token(
        {
            "sub": "smoke-script",
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "actor_type": "tenant_admin",
            "roles": ["tenant_admin"],
        }
    )


def enqueue_via_http(
    settings: Settings,
    token: str,
    tenant_id: UUID,
    idempotency_key: str,
) -> dict:
    host = "127.0.0.1" if settings.api_host in ("0.0.0.0", "") else settings.api_host
    url = f"http://{host}:{settings.api_port}/tenant/tasks"
    body = {
        "kind": "log_only",
        "payload": {"hello": "world", "source": "smoke"},
        "idempotency_key": idempotency_key,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(tenant_id),
    }
    resp = httpx.post(url, json=body, headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def wait_for_status(
    session_factory: sessionmaker[Session],
    task_id: UUID,
    *,
    target: str = "completed",
    timeout: float = 15.0,
) -> Task:
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        with session_factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise SystemExit(f"task {task_id} disappeared from app.task")
            last_status = task.status
            if task.status == target:
                session.expunge(task)
                return task
        time.sleep(0.2)
    raise SystemExit(
        f"task {task_id} did not reach status={target!r} within {timeout:.0f}s "
        f"(last seen: {last_status!r})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the task to reach 'completed'",
    )
    args = parser.parse_args()

    settings = get_settings()
    session_factory = get_session_factory(settings)

    print(f"[1/4] seeding tenant slug={SEED_TENANT_SLUG!r}")
    tenant = ensure_seed_tenant(session_factory)
    print(f"      tenant_id={tenant.id}")

    print("[2/4] encoding dev bearer token (role=tenant_admin)")
    token = make_dev_token(tenant)

    idem = f"smoke-{uuid4()}"
    print(f"[3/4] POST /tenant/tasks (kind=log_only, idempotency_key={idem})")
    enqueued = enqueue_via_http(settings, token, tenant.id, idem)
    task_id = UUID(enqueued["id"])
    print(f"      enqueued id={task_id} status={enqueued['status']}")

    print(f"[4/4] polling app.task for status=completed (timeout={args.timeout:.0f}s)")
    task = wait_for_status(session_factory, task_id, timeout=args.timeout)
    print("      SUCCESS")
    print(
        json.dumps(
            {
                "task_id": str(task.id),
                "tenant_id": str(task.tenant_id),
                "kind": task.kind,
                "status": task.status,
                "attempts": task.attempts,
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
