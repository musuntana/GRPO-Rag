from __future__ import annotations

from enum import StrEnum
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field
from ragos.core.errors import PermissionDeniedError


class ActorType(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    END_USER = "end_user"
    SERVICE = "service"


def build_scope_key(*, tenant_id: UUID, actor_id: str | None = None) -> str:
    """Encode a shared tenant scope, or a private scope for a trusted actor."""
    if not isinstance(tenant_id, UUID):
        raise PermissionDeniedError("unscoped scope_key identity: tenant_id must be a UUID")
    key = f"tenant/{tenant_id}"
    if actor_id is not None:
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise PermissionDeniedError("private scope requires an actor identity")
        key += f"/actor/{quote(actor_id, safe='')}"
    return key


class TenantContext(BaseModel):
    # Identity is immutable. Binding a server-resolved user creates a new context.
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    tenant_slug: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_type: ActorType = ActorType.SERVICE
    roles: list[str] = Field(default_factory=list)
    plan_tier: str = "standard"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    trace_id: str = Field(min_length=1)

    user_id: UUID | None = None
    brand_id: UUID | None = None
    allowed_kb_ids: list[UUID] | None = None
    model_binding_id: UUID | None = None
    security_level: Literal["public", "internal", "restricted"] = "internal"

    @computed_field
    @property
    def scope_key(self) -> str:
        return build_scope_key(tenant_id=self.tenant_id)

    def require_scope_key(self) -> str:
        return self.scope_key

    def require_private_scope_key(self) -> str:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise PermissionDeniedError("private scope requires an actor identity")
        return build_scope_key(tenant_id=self.tenant_id, actor_id=self.actor_id)

    def require_user_id(self) -> UUID:
        self.require_private_scope_key()
        if not isinstance(self.user_id, UUID):
            raise PermissionDeniedError("private resource requires a server-resolved user_id")
        return self.user_id

    def bind_user(self, user_id: UUID) -> TenantContext:
        """Bind a user after trusted tenant/actor ownership resolution by the caller."""
        self.require_private_scope_key()
        if not isinstance(user_id, UUID):
            raise PermissionDeniedError("user_id must be a server-resolved UUID")
        if self.user_id is not None and self.user_id != user_id:
            raise PermissionDeniedError("cannot rebind a context to another user")
        return self.model_copy(update={"user_id": user_id})

    def build_thread_id(self, workflow_id: str, run_id: str) -> str:
        self.require_scope_key()
        if not workflow_id or not run_id:
            raise PermissionDeniedError("thread id requires workflow and run identity")
        return f"{self.tenant_id}:{workflow_id}:{run_id}"
