"""Trusted worker embedding boundary for the explicit development ingest path.

Identity is frozen from server-side settings through the existing
``FrozenEmbeddingSpec``. Every model call still goes through the tenant
``ModelRouter``; this module is not a second gateway or a provider bypass.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable

from ragos.core.config import Settings
from ragos.core.errors import ProviderError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.model_gateway.local_admission import (
    LocalModelAdmission,
    LocalPinnedProvider,
    local_admission_from_settings,
)
from ragos.model_gateway.providers import ResolvedRoute
from ragos.model_gateway.router import ModelRouter
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos.storage.db import bind_session_tenant
from sqlmodel import Session

HASH_TEST_MODE = "test_hash"
LOCAL_GATEWAY_MODE = "dev_local_gateway"

_HASH_IDENTITY = ("hash-test", "sha256-hash-v1")


def worker_embedding_spec(settings: Settings) -> FrozenEmbeddingSpec:
    """Freeze the physical embedding identity from trusted settings only."""
    return FrozenEmbeddingSpec(
        model=settings.local_model_embed,
        model_digest=settings.local_model_embed_revision,
        dimension=settings.local_model_embed_dimension,
        profile=settings.embedding_profile,
    )


def validate_worker_embedding_settings(settings: Settings) -> str:
    """Select and validate the worker mode before opening any resource.

    The test/hash identity is unchanged. Real execution is development-only,
    requires both explicit opt-ins plus a valid local admission, and refuses
    non-Linux hosts before database, vector or model work.
    """
    if settings.app_env == "test":
        if not settings.task_test_embedding:
            raise ValidationError(
                "ingest process unavailable: real gateway embedding is not connected"
            )
        if (settings.embedding_profile, settings.embedding_version) != _HASH_IDENTITY:
            raise ValidationError(
                "test ingestion requires the explicit frozen hash embedding identity"
            )
        return HASH_TEST_MODE

    if settings.app_env != "dev":
        raise ValidationError(
            "ingest process unavailable: real gateway embedding is not connected"
        )
    if not settings.ingest_local_model_enabled:
        raise ValidationError(
            "development ingestion requires ingest_local_model_enabled=true"
        )
    if not settings.local_model_enabled:
        raise ValidationError("development ingestion requires local_model_enabled=true")
    if local_admission_from_settings(settings) is None:
        raise ValidationError(
            "development ingestion requires an admitted local model provider"
        )
    if sys.platform != "linux":
        raise ValidationError("development ingestion local model execution requires Linux")

    spec = worker_embedding_spec(settings)
    if (spec.profile, spec.version) != (
        settings.embedding_profile,
        settings.embedding_version,
    ):
        raise ValidationError(
            "configured embedding identity differs from the frozen local model spec"
        )
    return LOCAL_GATEWAY_MODE


class WorkerEmbedding:
    """Per-text embedding adapter bound to one tenant and one frozen identity."""

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Session],
        tenant_ctx: TenantContext,
    ) -> None:
        if settings.app_env != "dev" or not settings.ingest_local_model_enabled:
            raise ValidationError(
                "worker embedding requires an explicitly enabled development ingest gateway"
            )
        admission = local_admission_from_settings(settings)
        if admission is None:
            raise ValidationError(
                "worker embedding requires an admitted local model provider"
            )
        self._settings = settings
        self._session_factory = session_factory
        self._tenant_ctx = tenant_ctx
        self._admission: LocalModelAdmission = admission
        self.spec = worker_embedding_spec(settings)
        if (self.spec.profile, self.spec.version) != (
            settings.embedding_profile,
            settings.embedding_version,
        ):
            raise ValidationError(
                "configured embedding identity differs from the frozen local model spec"
            )
        if (
            self.spec.model != admission.embedding_model
            or self.spec.dimension != admission.embedding_dimension
        ):
            raise ValidationError(
                "frozen embedding identity differs from the admitted local provider"
            )

    @property
    def profile(self) -> str:
        return self.spec.profile

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    def _resolve_route(self) -> ResolvedRoute:
        with self._session_factory() as session:
            bind_session_tenant(session, self._tenant_ctx.tenant_id)
            with session.begin():
                router = ModelRouter(session, settings=self._settings)
                return router.resolve(self._tenant_ctx, "embed")

    def preflight(self) -> None:
        """Authorize the tenant route and pins without making a model request."""
        route = self._resolve_route()
        if not isinstance(route.provider, LocalPinnedProvider):
            raise ProviderError("tenant embed route is not the admitted local provider")
        if route.model_name != self.spec.model:
            raise ProviderError("tenant embed route model is not admitted")
        if route.parameters:
            raise ProviderError("tenant embedding parameters are not admitted")

    def embed_one(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("embedding input must be nonempty text")
        with self._session_factory() as session:
            bind_session_tenant(session, self._tenant_ctx.tenant_id)
            with session.begin():
                result = ModelRouter(session, settings=self._settings).embed(
                    self._tenant_ctx, texts=[text]
                )
        return self._validated_vector(result.embeddings)

    def _validated_vector(self, vectors) -> list[float]:
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise ProviderError(
                "embedding provider returned an invalid vector count"
            )
        vector = vectors[0]
        if (
            not isinstance(vector, list)
            or len(vector) != self.spec.dimension
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            )
            or not any(value != 0 for value in vector)
        ):
            raise ProviderError(
                "embedding differs from the pinned dimension or vector contract"
            )
        return [float(value) for value in vector]
