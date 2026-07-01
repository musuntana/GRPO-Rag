from __future__ import annotations

from uuid import UUID

from ragos.core.errors import ConflictError
from ragos.storage.models.app import Tenant
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import PlatformRepository
from sqlmodel import Session


class PlatformTenantRepository(PlatformRepository[Tenant]):
    """Cross-tenant repository for platform-admin CRUD on tenants.

    Must only be invoked from paths protected by ``require_platform_admin``.
    Mutations do not open their own transactions — the HTTP handler owns the
    transaction boundary.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session=session, model_type=Tenant)

    def list_all(self, *, limit: int = 100, offset: int = 0) -> list[Tenant]:
        return self.list(limit=limit, offset=offset)

    def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = self._select().where(Tenant.slug == slug)
        return self.session.exec(stmt).first()

    def create(self, *, slug: str, display_name: str) -> Tenant:
        if self.get_by_slug(slug) is not None:
            raise ConflictError("Tenant slug already exists", slug=slug)
        tenant = Tenant(slug=slug, display_name=display_name)
        self.session.add(tenant)
        self.session.flush()
        self.session.refresh(tenant)
        return tenant

    def update(
        self,
        tenant_id: UUID,
        *,
        display_name: str | None = None,
        is_active: bool | None = None,
    ) -> Tenant:
        tenant = self.require_by_id(tenant_id)
        if display_name is not None:
            tenant.display_name = display_name
        if is_active is not None:
            tenant.is_active = is_active
        tenant.updated_at = utc_now()
        self.session.add(tenant)
        self.session.flush()
        self.session.refresh(tenant)
        return tenant
