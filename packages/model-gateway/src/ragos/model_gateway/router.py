from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from ragos.core.config import get_settings
from ragos.core.errors import ProviderError, ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.model_gateway.key_pool import KeyPoolManager
from ragos.model_gateway.local_admission import (
    LocalModelAdmission,
    LocalPinnedProvider,
    local_admission_from_settings,
)
from ragos.model_gateway.profiles import ProfileResolver
from ragos.model_gateway.providers import ChatChunk, EmbedResult, RerankEntry, ResolvedRoute
from sqlmodel import Session

LOGGER = get_logger(__name__)


class ModelRouter:
    """Central routing facade — resolves tenant + use_case → provider call.

    Resolution chain::

        tenant_binding → profile → provider → key → provider instance

    Authorization precedes provider admission, which precedes key acquisition and
    provider construction. Calls use a single resolved route; retries and fallback
    traversal are not applied.
    """

    def __init__(
        self,
        session: Session,
        *,
        allow_test_stub: bool = False,
        local_model: LocalModelAdmission | None = None,
    ) -> None:
        settings = get_settings()
        if local_model is not None and settings.app_env != "dev":
            raise ValidationError("local model gateway may only run in development")
        if local_model is not None and settings.api_host not in {
            "localhost", "127.0.0.1", "::1"
        }:
            raise ValidationError("local model gateway requires loopback-bound API")
        self._session = session
        self._profiles = ProfileResolver(session)
        self._keys = KeyPoolManager(session, allow_test_stub=allow_test_stub)
        # The deployed default requires an explicit environment opt-in.
        # No tenant-controlled request field can turn on local model access.
        self._local = (
            local_model
            if local_model is not None
            else local_admission_from_settings(settings)
        )

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
        if self._local is not None and profile.provider_id == self._local.provider_id:
            # Tenant profile resolution above authorizes this use_case first.
            expected_model = self._local.model_for(use_case)
            if profile.model_name != expected_model:
                raise ProviderError("Local model profile is not admitted")
            if use_case == "embed" and profile.parameters:
                raise ProviderError("Local embedding parameters are not admitted")
            slug = self._keys.admit_local_provider(
                profile.provider_id, expected_base_url=self._local.base_url
            )
            provider = LocalPinnedProvider(self._local)
        else:
            # Ordinary provider credentials are NOT connected: never send
            # database ciphertext or unapproved URLs to the HTTP adapter.
            self._keys.admit_provider(profile.provider_id)
            lease = self._keys.acquire_key(profile.provider_id)
            provider = self._keys.build_provider(lease)
            slug = lease.provider_slug

        LOGGER.info(
            "model route resolved",
            extra={
                "tenant_slug": tenant_ctx.tenant_slug,
                "use_case": use_case,
                "provider": slug,
                "model": profile.model_name,
            },
        )

        return ResolvedRoute(
            provider=provider,
            provider_slug=slug,
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

    def rerank(
        self,
        tenant_ctx: TenantContext,
        *,
        query: str,
        documents: list[str],
        profile_override: UUID | None = None,
    ) -> list[RerankEntry]:
        """Tenant-bound model reranking; no generic direct provider bypass.

        Local Docker Model Runner admission deliberately does not permit
        reranking, because it has no verified /reranks route. Other providers
        must pass existing profile, entitlement and secret admission gates.
        Budget/cost accounting remains an explicit deployment blocker.
        """
        if not isinstance(query, str) or not 0 < len(query) <= 4000:
            raise ValidationError("rerank query must be a bounded nonempty string")
        if (
            not isinstance(documents, list)
            or not 1 <= len(documents) <= 50
            or any(not isinstance(d, str) or not 0 < len(d) <= 16000 for d in documents)
            or sum(len(d) for d in documents) > 160000
        ):
            raise ValidationError("rerank requires 1-50 bounded document passages")

        route = self.resolve(tenant_ctx, "rerank", profile_override=profile_override)
        try:
            entries = route.provider.rerank(
                query=query,
                documents=documents,
                model=route.model_name,
                parameters=route.parameters,
            )
        except Exception:
            LOGGER.exception(
                "rerank call failed",
                extra={"provider": route.provider_slug, "model": route.model_name},
            )
            raise
        if (
            not isinstance(entries, list)
            or len(entries) != len(documents)
            or any(not isinstance(entry, RerankEntry) for entry in entries)
        ):
            raise ProviderError("Reranker returned an incomplete candidate set")
        indices: set[int] = set()
        for entry in entries:
            if (
                type(entry.index) is not int
                or not 0 <= entry.index < len(documents)
                or entry.index in indices
                or type(entry.score) not in (int, float)
            ):
                raise ProviderError("Reranker returned invalid indices or scores")
            try:
                score = float(entry.score)
            except OverflowError:
                raise ProviderError("Reranker returned invalid indices or scores") from None
            if not math.isfinite(score):
                raise ProviderError("Reranker returned invalid indices or scores")
            indices.add(entry.index)
        return entries
