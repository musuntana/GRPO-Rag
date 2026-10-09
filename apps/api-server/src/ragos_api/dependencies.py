from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from ragos.auth.deps import get_auth_claims
from ragos.auth.jwt import AuthClaims
from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.db import bind_session_tenant, get_session_factory
from sqlmodel import Session


def get_db_session(request: Request) -> Iterator[Session]:
    """Return a DB session with RLS context injected for tenant-scoped requests.

    When the middleware has resolved a tenant (via X-Tenant-Id header, JWT
    claim, or Host header domain lookup), this dependency executes
    ``SET LOCAL app.current_tenant_id`` so that PostgreSQL Row-Level Security
    policies act as a defense-in-depth guard.
    """
    session_factory = get_session_factory()
    with session_factory() as session:
        rls_tenant_id = getattr(request.state, "rls_tenant_id", None)
        if rls_tenant_id is not None:
            bind_session_tenant(session, rls_tenant_id)
        yield session


def get_tenant_ctx(request: Request) -> TenantContext:
    tenant_ctx = getattr(request.state, "tenant_ctx", None)
    if tenant_ctx is None:
        raise ValidationError(
            "Tenant context is not available on request.state",
            source="request.state.tenant_ctx",
        )
    return tenant_ctx


DBSession = Annotated[Session, Depends(get_db_session)]
TenantCtx = Annotated[TenantContext, Depends(get_tenant_ctx)]
AuthCtx = Annotated[AuthClaims, Depends(get_auth_claims)]
