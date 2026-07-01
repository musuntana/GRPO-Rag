from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from ragos.core.errors import NotFoundError
from ragos.core.tenant import TenantContext
from sqlmodel import Session, SQLModel, select

ModelT = TypeVar("ModelT", bound=SQLModel)


class TenantRepository(Generic[ModelT]):
    """Repository for tenant-scoped tables.

    Every query is rewritten to include ``tenant_id = tenant_ctx.tenant_id``.
    Callers must provide a ``TenantContext``.
    """

    def __init__(
        self,
        session: Session,
        tenant_ctx: TenantContext,
        model_type: type[ModelT],
    ) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx
        self.model_type = model_type

    def scoped_statement(self):
        return select(self.model_type).where(
            self.model_type.tenant_id == self.tenant_ctx.tenant_id
        )

    def get_by_id(self, record_id: UUID) -> ModelT | None:
        statement = self.scoped_statement().where(self.model_type.id == record_id)
        return self.session.exec(statement).first()

    def require_by_id(self, record_id: UUID) -> ModelT:
        record = self.get_by_id(record_id)
        if record is None:
            raise NotFoundError(
                f"{self.model_type.__name__} not found",
                resource=self.model_type.__name__,
                id=str(record_id),
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        return record

    def list(self, limit: int = 100, offset: int = 0) -> list[ModelT]:
        statement = self.scoped_statement().offset(offset).limit(limit)
        return list(self.session.exec(statement).all())

    def add(self, record: ModelT) -> ModelT:
        self.session.add(record)
        return record


class PlatformRepository(Generic[ModelT]):
    """Repository for platform-admin paths that must see across tenants.

    This class deliberately does NOT inject a tenant filter — callers are
    expected to be privileged (``platform_admin`` role, or internal worker
    processes). When querying tenant-scoped tables, callers must pass an
    explicit ``tenant_id`` when they want a narrow scope.
    """

    def __init__(self, session: Session, model_type: type[ModelT]) -> None:
        self.session = session
        self.model_type = model_type

    def _select(self):
        return select(self.model_type)

    def get_by_id(self, record_id: UUID) -> ModelT | None:
        statement = self._select().where(self.model_type.id == record_id)
        return self.session.exec(statement).first()

    def require_by_id(self, record_id: UUID) -> ModelT:
        record = self.get_by_id(record_id)
        if record is None:
            raise NotFoundError(
                f"{self.model_type.__name__} not found",
                resource=self.model_type.__name__,
                id=str(record_id),
            )
        return record

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        tenant_id: UUID | None = None,
    ) -> list[ModelT]:
        statement = self._select().offset(offset).limit(limit)
        if tenant_id is not None and hasattr(self.model_type, "tenant_id"):
            statement = statement.where(self.model_type.tenant_id == tenant_id)
        return list(self.session.exec(statement).all())

    def add(self, record: ModelT) -> ModelT:
        self.session.add(record)
        return record
