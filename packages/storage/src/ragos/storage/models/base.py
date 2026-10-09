from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, MetaData
from sqlmodel import Field, SQLModel

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class RagosModel(SQLModel):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey(SQLModel):
    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)


class Timestamped(SQLModel):
    created_at: datetime = Field(
        default_factory=utc_now, nullable=False, sa_type=DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, nullable=False, sa_type=DateTime(timezone=True)
    )


class TenantScoped(SQLModel):
    tenant_id: UUID = Field(nullable=False, index=True)
