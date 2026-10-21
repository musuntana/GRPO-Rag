"""Explicit development-only admission for local OpenAI-compatible model traffic.

This is NOT production model onboarding. Only a locally bound model endpoint,
one platform provider identity and two pinned model names may be used.
No encrypted key-pool rows are decrypted or forwarded to a provider.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ragos.core.config import Settings
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers import ChatChunk, EmbedResult
from ragos.model_gateway.providers.openai_compatible import (
    JsonTransport,
    OpenAICompatibleProvider,
)

LOCAL_PROVIDER_TYPE = "local_openai"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "model-runner.docker.internal"}


@dataclass(frozen=True, slots=True)
class LocalModelAdmission:
    provider_id: UUID
    base_url: str
    chat_model: str
    embedding_model: str
    embedding_dimension: int
    transport: JsonTransport | None = None

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            url.scheme != "http"
            or url.hostname not in _LOCAL_HOSTS
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or not url.path.rstrip("/").endswith("/v1")
        ):
            raise ValidationError("dev model gateway permits local HTTP /v1 only")
        if (
            not self.chat_model.strip()
            or not self.embedding_model.strip()
            or not 1 <= self.embedding_dimension <= 8192
        ):
            raise ValidationError("dev model gateway requires pinned model names and dimension")

    def model_for(self, use_case: str) -> str:
        if use_case == "chat":
            return self.chat_model
        if use_case == "embed":
            return self.embedding_model
        raise ProviderError("Local model use case is not admitted")


def local_admission_from_settings(settings: Settings) -> LocalModelAdmission | None:
    """No opt-in means no extra capability. Explicit opt-in is dev-only."""
    if not settings.local_model_enabled:
        return None
    if settings.app_env != "dev":
        raise ValidationError("local model gateway may only run in development")
    if settings.api_host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValidationError("local model gateway requires loopback-bound API")
    if settings.local_model_provider_id is None:
        raise ValidationError("local model provider ID is required")
    return LocalModelAdmission(
        provider_id=settings.local_model_provider_id,
        base_url=settings.local_model_base_url,
        chat_model=settings.local_model_chat,
        embedding_model=settings.local_model_embed,
        embedding_dimension=settings.local_model_embed_dimension,
    )


class LocalPinnedProvider:
    """Constrain and validate a single local model call before/after HTTP."""

    def __init__(self, config: LocalModelAdmission) -> None:
        self._config = config
        self._provider = OpenAICompatibleProvider(
            base_url=config.base_url, timeout_seconds=30, transport=config.transport
        )

    def chat(
        self, *, messages: list[dict[str, Any]], model: str,
        parameters: dict[str, Any] | None = None,
    ) -> ChatChunk:
        if model != self._config.chat_model:
            raise ProviderError("Unapproved local chat model")
        if (
            not 1 <= len(messages) <= 32
            or sum(len(str(m.get("content", ""))) for m in messages) > 32000
        ):
            raise ValidationError("local chat exceeds development request limits")
        args = dict(parameters or {})
        tokens = args.get("max_tokens", 256)
        if type(tokens) is not int or not 1 <= tokens <= 512:
            raise ValidationError("local chat max_tokens must be 1-512")
        args["max_tokens"] = tokens
        return self._provider.chat(messages=messages, model=model, parameters=args)

    def embed(
        self, *, texts: list[str], model: str,
        parameters: dict[str, Any] | None = None,
    ) -> EmbedResult:
        if model != self._config.embedding_model:
            raise ProviderError("Unapproved local embedding model")
        if (
            not 1 <= len(texts) <= 16
            or any(not isinstance(t, str) or not 1 <= len(t) <= 8000 for t in texts)
            or parameters
        ):
            raise ValidationError("local embedding exceeds development request limits")
        result = self._provider.embed(texts=texts, model=model)
        for vector in result.embeddings:
            if (
                len(vector) != self._config.embedding_dimension
                or any(not math.isfinite(x) for x in vector)
                or not any(x != 0 for x in vector)
            ):
                raise ProviderError("Local embedding dimension or numeric contract failed")
        return result

    def rerank(
        self, *, query: str, documents: list[str], model: str,
        parameters: dict[str, Any] | None = None,
    ):
        raise ProviderError("Local reranking is not admitted")

    def health(self) -> bool:
        return self._provider.health()
