from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ActorType(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    END_USER = "end_user"
    SERVICE = "service"


class TenantContext(BaseModel):
    tenant_id: UUID
    tenant_slug: str
    actor_id: str
    actor_type: ActorType = ActorType.SERVICE
    roles: list[str] = Field(default_factory=list)
    plan_tier: str = "standard"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    trace_id: str

    brand_id: UUID | None = None
    allowed_kb_ids: list[UUID] | None = None
    model_binding_id: UUID | None = None
    security_level: Literal["public", "internal", "restricted"] = "internal"

    def build_thread_id(self, workflow_id: str, run_id: str) -> str:
        return f"{self.tenant_id}:{workflow_id}:{run_id}"
