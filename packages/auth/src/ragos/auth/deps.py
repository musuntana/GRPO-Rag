from __future__ import annotations

from collections.abc import Iterable

from fastapi import Request
from ragos.auth.jwt import AuthClaims
from ragos.auth.rbac import has_any_role
from ragos.core.errors import AuthenticationError, PermissionDeniedError


def get_auth_claims(request: Request) -> AuthClaims:
    claims = getattr(request.state, "auth_claims", None)
    if claims is None:
        raise AuthenticationError()
    return claims


def require_roles(request: Request, required_roles: Iterable[str]) -> AuthClaims:
    claims = get_auth_claims(request)
    required = list(required_roles)
    if not has_any_role(claims, required):
        raise PermissionDeniedError(
            "Actor does not have a required role",
            required_roles=required,
            actual_roles=claims.roles,
        )
    return claims


def require_tenant_admin(request: Request) -> AuthClaims:
    return require_roles(request, {"tenant_admin", "platform_admin"})


def require_platform_admin(request: Request) -> AuthClaims:
    return require_roles(request, {"platform_admin"})
