from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatChunk:
    content: str
    finish_reason: str | None = None
    model: str = ""
    usage: dict[str, int] | None = None


@dataclass(slots=True)
class EmbedResult:
    embeddings: list[list[float]]
    model: str
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RerankEntry:
    index: int
    score: float
    document: str | None = None


class BaseProvider(Protocol):
    """Protocol for LLM provider implementations.

    Each provider handles chat, embed, and rerank operations by
    calling the provider's native API. The provider is configured
    with a specific ``api_key`` and ``base_url`` at construction time.
    """

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> ChatChunk: ...

    def embed(
        self,
        *,
        texts: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> EmbedResult: ...

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[RerankEntry]: ...

    def health(self) -> bool: ...


# ---------------------------------------------------------------------------
# Resolved route
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedRoute:
    """The output of ModelRouter.resolve — everything needed to call a provider."""

    provider: BaseProvider
    provider_slug: str
    model_name: str
    parameters: dict[str, Any]
    profile_id: str
