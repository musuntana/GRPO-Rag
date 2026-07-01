from __future__ import annotations

from uuid import UUID

from ragos.core.errors import ConflictError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import User
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import PlatformRepository, TenantRepository
from sqlmodel import Session


class UserRepository(TenantRepository[User]):
    """Tenant-scoped user CRUD. All queries auto-filter to tenant_ctx.tenant_id."""

    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=User)

    def get_by_external_id(self, external_id: str) -> User | None:
        stmt = self.scoped_statement().where(User.external_id == external_id)
        return self.session.exec(stmt).first()

    def create(
        self,
        *,
        external_id: str,
        display_name: str,
        email: str | None = None,
        roles: list[str] | None = None,
    ) -> User:
        if self.get_by_external_id(external_id) is not None:
            raise ConflictError(
                "User external_id already exists in this tenant",
                external_id=external_id,
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        user = User(
            tenant_id=self.tenant_ctx.tenant_id,
            external_id=external_id,
            display_name=display_name,
            email=email,
            roles=roles or [],
        )
        self.session.add(user)
        self.session.flush()
        self.session.refresh(user)
        return user

    def update(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        email: str | None = None,
        roles: list[str] | None = None,
        is_active: bool | None = None,
    ) -> User:
        user = self.require_by_id(user_id)
        if display_name is not None:
            user.display_name = display_name
        if email is not None:
            user.email = email
        if roles is not None:
            user.roles = roles
        if is_active is not None:
            user.is_active = is_active
        user.updated_at = utc_now()
        self.session.add(user)
        self.session.flush()
        self.session.refresh(user)
        return user


class PlatformUserRepository(PlatformRepository[User]):
    """Cross-tenant user CRUD for platform admin paths."""

    def __init__(self, session: Session) -> None:
        super().__init__(session=session, model_type=User)

    def list_for_tenant(
        self, tenant_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[User]:
        return self.list(limit=limit, offset=offset, tenant_id=tenant_id)

    def create(
        self,
        *,
        tenant_id: UUID,
        external_id: str,
        display_name: str,
        email: str | None = None,
        roles: list[str] | None = None,
    ) -> User:
        stmt = self._select().where(
            User.tenant_id == tenant_id, User.external_id == external_id
        )
        existing = self.session.exec(stmt).first()
        if existing is not None:
            raise ConflictError(
                "User external_id already exists in this tenant",
                external_id=external_id,
                tenant_id=str(tenant_id),
            )
        user = User(
            tenant_id=tenant_id,
            external_id=external_id,
            display_name=display_name,
            email=email,
            roles=roles or [],
        )
        self.session.add(user)
        self.session.flush()
        self.session.refresh(user)
        return user
