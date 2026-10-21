from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import NotFoundError, ProviderError
from ragos.core.logging import get_logger
from ragos.model_gateway.providers import BaseProvider
from ragos.storage.models.app import KeyPool
from ragos.storage.models.app import Provider as ProviderModel
from ragos.storage.repositories.base import PlatformRepository
from sqlmodel import Session

LOGGER = get_logger(__name__)

UNAVAILABLE_MESSAGE = "Model provider unavailable"
TEST_STUB_TYPE = "stub"


@dataclass(slots=True)
class KeyLease:
    key_id: UUID
    provider_type: str
    provider_slug: str
    base_url: str | None
    provider_kwargs: dict


class KeyPoolManager:
    """Weighted round-robin key selection with health tracking.

    Resolves a provider's active keys, picks one by weight, and
    constructs a provider instance. Tracks failures so unhealthy
    keys are eventually disabled.
    """

    def __init__(self, session: Session, *, allow_test_stub: bool = False) -> None:
        self.session = session
        self._allow_test_stub = allow_test_stub
        self._key_repo = PlatformRepository(session=session, model_type=KeyPool)
        self._provider_repo = PlatformRepository(session=session, model_type=ProviderModel)
        self._round_robin: dict[UUID, int] = {}

    def admit_provider(self, provider_id: UUID) -> None:
        """Refuse an unavailable provider before any key is read.

        Reads only the provider configuration row. Missing, inactive, unconnected
        provider types and stubs without explicit test opt-in are all refused with
        the same generic error, so the response never reveals which condition held.
        """
        provider = self._provider_repo.get_by_id(provider_id)
        if provider is None or not provider.is_active:
            raise ProviderError(UNAVAILABLE_MESSAGE)
        if not self._is_connected(provider.provider_type):
            raise ProviderError(UNAVAILABLE_MESSAGE)

    def admit_local_provider(self, provider_id: UUID, *, expected_base_url: str) -> str:
        """Authorize a pinned local provider without reading any key-pool row.

        Both the platform provider ID and exact URL must match operator-side
        configuration, and the provider must be explicitly active.
        """
        provider = self._provider_repo.get_by_id(provider_id)
        if (
            provider is None
            or not provider.is_active
            or provider.provider_type != "local_openai"
            or provider.base_url != expected_base_url
        ):
            raise ProviderError(UNAVAILABLE_MESSAGE)
        return provider.slug

    def acquire_key(self, provider_id: UUID) -> KeyLease:
        """Select the next active key for *provider_id* by weighted
        round-robin. Raises ``ProviderError`` if no active keys exist."""
        provider = self._require_provider(provider_id)
        keys = self._list_active_keys(provider_id)
        if not keys:
            raise ProviderError("No active keys available for provider")

        weights = [k.weight for k in keys]
        total = sum(weights)
        idx = self._round_robin.get(provider_id, 0) % total
        cursor = 0
        chosen = keys[0]
        for key in keys:
            cursor += key.weight
            if idx < cursor:
                chosen = key
                break

        self._round_robin[provider_id] = (idx + 1) % total

        return KeyLease(
            key_id=chosen.id,
            provider_type=provider.provider_type,
            provider_slug=provider.slug,
            base_url=provider.base_url,
            provider_kwargs={
                "api_key": chosen.api_key_encrypted,
                "base_url": provider.base_url,
            },
        )

    def release_key(self, lease: KeyLease, *, success: bool) -> None:
        """Update key health. Consecutive failures are tracked."""
        key = self._key_repo.get_by_id(lease.key_id)
        if key is None:
            return

        now_utc = __import__("datetime").datetime.now(__import__("datetime").UTC)
        if success:
            key.consecutive_failures = 0
            key.last_health_check_at = now_utc
        else:
            key.consecutive_failures += 1
            key.last_health_check_at = now_utc
            if key.consecutive_failures >= 5:
                key.is_active = False
                LOGGER.warning(
                    "disabling unhealthy key",
                    extra={
                        "key_id": str(key.id),
                        "provider_id": str(key.provider_id),
                    },
                )
        self.session.add(key)
        self.session.flush()

    def _is_connected(self, provider_type: str) -> bool:
        return self._allow_test_stub and provider_type == TEST_STUB_TYPE

    def _require_provider(self, provider_id: UUID) -> ProviderModel:
        try:
            return self._provider_repo.require_by_id(provider_id)
        except NotFoundError:
            raise ProviderError(UNAVAILABLE_MESSAGE) from None

    def _list_active_keys(self, provider_id: UUID) -> list[KeyPool]:
        from sqlmodel import select as sql_select

        stmt = (
            sql_select(KeyPool)
            .where(KeyPool.provider_id == provider_id)
            .where(KeyPool.is_active == True)  # noqa: E712
        )
        return list(self.session.exec(stmt).all())

    def build_provider(self, lease: KeyLease) -> BaseProvider:
        """Refuse unconnected capabilities; a stub is explicit test data only."""
        if not self._is_connected(lease.provider_type):
            raise ProviderError(UNAVAILABLE_MESSAGE)
        from ragos.model_gateway.providers.stub import StubProvider

        # Stub execution must never receive a stored encrypted key or network URL.
        return StubProvider(api_key="test-only-placeholder")
