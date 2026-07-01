from __future__ import annotations

import base64
import json
from typing import Any

from pydantic import BaseModel, Field
from ragos.core.errors import AuthenticationError, ValidationError
from ragos.core.tenant import ActorType


class AuthClaims(BaseModel):
    sub: str
    tenant_id: str | None = None
    tenant_slug: str | None = None
    actor_type: ActorType = ActorType.SERVICE
    roles: list[str] = Field(default_factory=list)
    plan_tier: str = "standard"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def encode_dev_token(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("utf-8").rstrip("=")
    return f"dev.{encoded}"


def decode_bearer_token(authorization: str | None) -> AuthClaims:
    if not authorization:
        raise AuthenticationError()

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Unsupported authorization scheme")

    if not token.startswith("dev."):
        raise AuthenticationError("Only dev bearer tokens are supported right now")

    try:
        payload = json.loads(_decode_base64url(token[4:]).decode("utf-8"))
        return AuthClaims.model_validate(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("Invalid dev bearer token", token=token) from exc
