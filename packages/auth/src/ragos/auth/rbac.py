from __future__ import annotations

from collections.abc import Iterable

from ragos.auth.jwt import AuthClaims


def has_any_role(claims: AuthClaims, required_roles: Iterable[str]) -> bool:
    required = set(required_roles)
    return bool(required.intersection(claims.roles))
