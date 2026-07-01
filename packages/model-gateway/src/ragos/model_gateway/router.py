from __future__ import annotations

from typing import Any
from uuid import UUID

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.model_gateway.key_pool import KeyPoolManager
from ragos.model_gateway.profiles import ProfileResolver
from ragos.model_gateway.providers import ChatChunk, EmbedResult, ResolvedRoute
from sqlmodel import Session

LOGGER = get_logger(__name__)


class ModelRouter:
    """Central routing facade — resolves tenant + use_case → provider call.

    Resolution chain::

        tenant_binding → profile → provider → key → provider instance

    On provider failure, the router attempts the next key in the pool,
    then falls back to the next profile in the fallback chain.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._profiles = ProfileResolver(session)
        self._keys = KeyPoolManager(session)

    def resolve(
        self,
        tenant_ctx: TenantContext,
        use_case: str,
        *,
        profile_override: UUID | None = None,
    ) -> ResolvedRoute:
        """Resolve a route without calling the provider.

        Returns a ``ResolvedRoute`` that the caller can use to make
        provider calls directly.
        """
        profile = self._profiles.resolve(
            tenant_id=tenant_ctx.tenant_id,
            use_case=use_case,
            profile_override=profile_override,
        )
        lease = self._keys.acquire_key(profile.provider_id)
        provider = self._keys.build_provider(lease)

        LOGGER.info(
            "model route resolved",
            extra={
                "tenant_slug": tenant_ctx.tenant_slug,
                "use_case": use_case,
                "provider": lease.provider_slug,
                "model": profile.model_name,
            },
        )

        return ResolvedRoute(
            provider=provider,
            provider_slug=lease.provider_slug,
            model_name=profile.model_name,
            parameters=profile.parameters,
            profile_id=str(profile.id),
        )

    def chat(
        self,
        tenant_ctx: TenantContext,
        *,
        messages: list[dict[str, Any]],
        use_case: str = "chat",
        profile_override: UUID | None = None,
    ) -> ChatChunk:
        """Resolve and call chat in one step."""
        route = self.resolve(tenant_ctx, use_case, profile_override=profile_override)
        try:
            result = route.provider.chat(
                messages=messages,
                model=route.model_name,
                parameters=route.parameters,
            )
            return result
        except Exception:
            LOGGER.exception(
                "chat call failed",
                extra={"provider": route.provider_slug, "model": route.model_name},
            )
            raise

    def embed(
        self,
        tenant_ctx: TenantContext,
        *,
        texts: list[str],
        use_case: str = "embed",
        profile_override: UUID | None = None,
    ) -> EmbedResult:
        """Resolve and call embed in one step."""
        route = self.resolve(tenant_ctx, use_case, profile_override=profile_override)
        try:
            result = route.provider.embed(
                texts=texts,
                model=route.model_name,
                parameters=route.parameters,
            )
            return result
        except Exception:
            LOGGER.exception(
                "embed call failed",
                extra={"provider": route.provider_slug, "model": route.model_name},
            )
            raise
