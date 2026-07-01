from __future__ import annotations

from uuid import uuid4

from ragos.auth.jwt import decode_bearer_token
from ragos.core.errors import RagOSError
from ragos.storage.db import get_session_factory
from ragos.tenant.resolver import resolve_tenant_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RequestContextMiddleware(BaseHTTPMiddleware):
    public_prefixes = ("/healthz", "/docs", "/openapi.json", "/redoc")
    # Tenant-scoped paths require a fully resolved TenantContext.
    tenant_prefixes = ("/tenant", "/chat")
    # Platform-admin paths only need authentication — they operate across
    # tenants and must not force the caller to supply a tenant header.
    platform_prefixes = ("/platform",)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id", str(uuid4()))
        request.state.request_id = request_id

        try:
            path = request.url.path
            if path.startswith(self.tenant_prefixes):
                claims = decode_bearer_token(request.headers.get("Authorization"))
                request.state.auth_claims = claims
                # Open a short-lived session for domain-based tenant resolution.
                session_factory = get_session_factory()
                with session_factory() as session:
                    request.state.tenant_ctx = resolve_tenant_context(
                        request,
                        claims,
                        trace_id=request_id,
                        session=session,
                    )
                # Store tenant_id for RLS injection in the DB session dependency.
                request.state.rls_tenant_id = request.state.tenant_ctx.tenant_id
            elif path.startswith(self.platform_prefixes):
                claims = decode_bearer_token(request.headers.get("Authorization"))
                request.state.auth_claims = claims
        except RagOSError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.to_response_content(),
                headers={"X-Request-Id": request_id},
            )

        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
