"""Caller-supplied scope identifiers are discarded, never filtered on.

A request body, query parameter, or unverified header may name a tenant, brand,
knowledge base, user, or model binding. None of those are inputs to the scope
decision — the scope is composed once in trusted server code from the resolved
identity and stored on ``tenant_ctx`` (ADR-0005).

Two things happen here, and the difference matters:

- a value that contradicts the derived scope is a **rejected request**. Silently
  dropping it would let a caller believe they queried something they did not,
  which is worse than an error: the response looks successful and the scope they
  intended was never applied.
- a value that happens to match is ignored rather than honoured. It is not
  evidence of anything; the server already knew.

``scope_key`` gets no comparison branch. It is server-side state by
construction, so its presence in a payload is an attempt to set identity from the
outside and is refused outright.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from ragos.core.errors import PermissionDeniedError
from ragos.core.tenant import TenantContext

#: Fields that describe scope rather than request content.
SCOPE_FIELDS = ("tenant_id", "brand_id", "kb_id", "user_id", "model_binding_id")

#: Always refused, matching or not.
SERVER_ONLY_FIELDS = ("scope_key",)


def discard_scope_fields(
    payload: dict[str, Any],
    tenant_ctx: TenantContext,
    *,
    where: str,
) -> None:
    """Validate any scope identifier present in ``payload`` against the server scope."""
    for field in SERVER_ONLY_FIELDS:
        if field in payload:
            raise PermissionDeniedError(
                "client-supplied scope fields are not accepted; scope is derived server-side",
                field=field,
                where=where,
            )

    for field in SCOPE_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        if field == "kb_id":
            # A knowledge base is a collection, not an identity: the allowed set
            # on the context cannot validate one value from a body, so it is
            # refused rather than intersected. Callers narrow scope through
            # ``tenant_ctx.allowed_kb_ids``, which only trusted code sets.
            raise PermissionDeniedError(
                "knowledge base selection comes from the resolved tenant scope, "
                "not from the request body",
                field=field,
                where=where,
            )
        expected = _derived_value(field, tenant_ctx)
        if expected is None:
            # The server has no value on this dimension yet, so a client value
            # cannot be checked — and an unverifiable value is refused rather
            # than assumed harmless.
            raise PermissionDeniedError(
                "client-supplied scope field cannot be verified against the resolved scope",
                field=field,
                where=where,
            )
        if str(payload[field]) != str(expected):
            raise PermissionDeniedError(
                "client-supplied scope field does not match the resolved scope",
                field=field,
                where=where,
            )


def _derived_value(field: str, tenant_ctx: TenantContext) -> Any:
    if field == "tenant_id":
        return tenant_ctx.tenant_id
    if field == "brand_id":
        return tenant_ctx.brand_id
    if field == "user_id":
        return tenant_ctx.actor_id
    if field == "model_binding_id":
        return tenant_ctx.model_binding_id
    return None


#: Methods that can carry a scope identifier in their body.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


async def enforce_body_scope(request: Request) -> None:
    """Router-level guard: reject a body that names its own scope.

    This runs on the raw JSON rather than on the parsed model, because a Pydantic
    model configured to ignore unknown fields would drop ``tenant_id`` before any
    route code saw it. A dropped field looks identical to an absent one, which is
    exactly the ambiguity the rule exists to remove: the caller cannot tell
    whether the server honoured the scope it asked for or never received it.
    """
    if request.method not in _BODY_METHODS:
        return
    content_type = request.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return

    try:
        payload = await request.json()
    except Exception:
        # A malformed body is the parser's problem to report, not ours to
        # reinterpret as a scope violation.
        return
    if not isinstance(payload, dict):
        return

    tenant_ctx = getattr(request.state, "tenant_ctx", None)
    if tenant_ctx is None:
        return

    discard_scope_fields(payload, tenant_ctx, where=request.url.path)
