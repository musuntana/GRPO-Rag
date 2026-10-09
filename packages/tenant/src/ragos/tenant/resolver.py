from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from ragos.auth.jwt import AuthClaims
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext

if TYPE_CHECKING:
    from sqlmodel import Session


def resolve_tenant_context(
    request: Request,
    claims: AuthClaims,
    *,
    trace_id: str,
    session: Session | None = None,
) -> TenantContext:
    header_tenant = request.headers.get("X-Tenant-Id")
    if header_tenant and header_tenant != claims.tenant_id:
        raise PermissionDeniedError("tenant header does not match authenticated tenant")
    tenant_id = claims.tenant_id

    # Domain-based resolution: derive tenant from Host header when
    # neither X-Tenant-Id nor JWT claim provides a tenant identifier.
    if not tenant_id and session is not None:
        host = request.headers.get("Host", "")
        domain = host.split(":")[0] if host else ""
        if domain:
            from ragos.storage.models.app import TenantDomain
            from sqlmodel import select

            stmt = select(TenantDomain).where(
                TenantDomain.domain == domain,
                TenantDomain.is_verified == True,  # noqa: E712
            )
            td = session.exec(stmt).first()
            if td is not None:
                tenant_id = str(td.tenant_id)

    if not tenant_id:
        raise ValidationError("Missing tenant identifier")

    tenant_slug = claims.tenant_slug or tenant_id

    return TenantContext(
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        actor_id=claims.sub,
        actor_type=claims.actor_type,
        roles=claims.roles,
        plan_tier=claims.plan_tier,
        locale=claims.locale,
        timezone=claims.timezone,
        trace_id=trace_id,
    )


def resolve_actor_user_id(session: Session, tenant_ctx: TenantContext):
    """Resolve a local user primary key inside the actor's authorized tenant."""
    from ragos.storage.repositories.users import UserRepository

    user = UserRepository(session, tenant_ctx).get_by_external_id(tenant_ctx.actor_id)
    if user is None or not user.is_active:
        return None
    return user.id
