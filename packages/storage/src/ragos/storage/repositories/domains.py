from __future__ import annotations

from ragos.core.tenant import TenantContext
from ragos.storage.models.app import TenantDomain
from ragos.storage.repositories.base import TenantRepository
from sqlmodel import Session, select


class TenantDomainRepository(TenantRepository[TenantDomain]):
    """Repository for tenant domain resolution and management."""

    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=TenantDomain)

    def get_by_domain(self, domain: str) -> TenantDomain | None:
        """Look up a domain globally — used by the domain resolution path.

        This deliberately bypasses tenant scoping because the resolver needs
        to find the owning tenant before knowing which TenantContext to build.
        """
        stmt = select(TenantDomain).where(TenantDomain.domain == domain)
        return self.session.exec(stmt).first()
