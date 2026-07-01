from __future__ import annotations

from typing import Any

from ragos.model_gateway.providers import ChatChunk, EmbedResult, RerankEntry


class StubProvider:
    """Provider stub that returns deterministic empty responses.

    Used in phase 1 to exercise the full routing pipeline without
    live API keys. Returns empty strings / zero vectors.
    """

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._healthy = True

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> ChatChunk:
        return ChatChunk(content="", model=model)

    def embed(
        self,
        *,
        texts: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> EmbedResult:
        dim = parameters.get("dimensions", 1536) if parameters else 1536
        return EmbedResult(
            embeddings=[[0.0] * dim for _ in texts],
            model=model,
        )

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[RerankEntry]:
        return [
            RerankEntry(index=i, score=0.0, document=doc)
            for i, doc in enumerate(documents)
        ]

    def health(self) -> bool:
        return self._healthy
