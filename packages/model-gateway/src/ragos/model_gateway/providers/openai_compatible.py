"""Minimal synchronous OpenAI-compatible model transport for S1 local validation.

This adapter makes real HTTP requests, but is NOT registered with the tenant
router: the key pool currently stores encrypted secrets with no verified secret
resolver. Registering those ciphertexts as bearer tokens would be unsafe.
The explicit local probe is the only current caller.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers import ChatChunk, EmbedResult, RerankEntry

JsonTransport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ALLOWED_LOCAL_HTTP_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "model-runner.docker.internal"}
)
_CHAT_PARAMETERS = frozenset(
    {
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "response_format",
        "seed",
    }
)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/v1")
    ):
        raise ValidationError("model base_url must be an explicit /v1 HTTP(S) endpoint")
    if parsed.scheme == "http" and parsed.hostname not in _ALLOWED_LOCAL_HTTP_HOSTS:
        raise ValidationError("plaintext model HTTP is limited to known local hosts")
    return value.rstrip("/")


def _http_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float):
    """Disable redirects and ambient proxy settings, enforce size and deadline."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ProviderError("Model provider response too large")
            parsed = json.loads(body.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError):
        # Do not expose provider response bodies, credentials, internal URLs or prompts.
        raise ProviderError("Model provider request failed") from None
    if not isinstance(parsed, dict):
        raise ProviderError("Model provider returned an invalid response")
    return parsed


def _safe_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ProviderError("Model provider returned an invalid numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ProviderError("Model provider returned a non-finite numeric value")
    return result


class OpenAICompatibleProvider:
    """Real Chat/Embedding HTTP calls plus optional provider-specific /reranks.

    The caller selects a trusted base URL and supplies a decrypted secret from
    an approved secret source. This class never looks up tenant bindings or
    interprets DB ciphertext. It is not a substitute for the auth/budget gates.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 20.0,
        transport: JsonTransport | None = None,
    ) -> None:
        self.base_url = _base_url(base_url)
        if not 0 < timeout_seconds <= 120:
            raise ValidationError("model timeout must be between 0 and 120 seconds")
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._transport = transport or _http_json

    def _request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            data = self._transport(
                f"{self.base_url}/{endpoint}", payload, headers, self.timeout_seconds
            )
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Model provider request failed") from None
        if not isinstance(data, dict):
            raise ProviderError("Model provider returned an invalid response")
        return data

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> ChatChunk:
        if not model or not messages:
            raise ValidationError("chat requires a model and nonempty messages")
        options = parameters or {}
        if set(options) - _CHAT_PARAMETERS or options.get("stream") is True:
            raise ValidationError("unsupported chat parameters")
        data = self._request(
            "chat/completions", {"model": model, "messages": messages, **options, "stream": False}
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            if not isinstance(content, str):
                raise TypeError
            usage = data.get("usage")
            if usage is not None and (
                not isinstance(usage, dict)
                or any(type(v) is not int or v < 0 for v in usage.values())
            ):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Invalid chat completion response") from None
        return ChatChunk(
            content=content, model=model, finish_reason=finish_reason, usage=usage
        )

    def embed(
        self,
        *,
        texts: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> EmbedResult:
        if not model or not texts or any(not isinstance(t, str) or not t for t in texts):
            raise ValidationError("embedding requires a model and nonempty texts")
        options = parameters or {}
        if set(options) - {"dimensions"}:
            raise ValidationError("unsupported embedding parameters")
        if "dimensions" in options and (
            type(options["dimensions"]) is not int or options["dimensions"] <= 0
        ):
            raise ValidationError("embedding dimensions must be positive")
        data = self._request(
            "embeddings", {"model": model, "input": texts, "encoding_format": "float", **options}
        )
        try:
            entries = data["data"]
            if not isinstance(entries, list) or len(entries) != len(texts):
                raise TypeError
            ordered: list[list[float] | None] = [None] * len(texts)
            dimension: int | None = None
            for entry in entries:
                idx = entry["index"]
                raw_vector = entry["embedding"]
                if (
                    type(idx) is not int or not 0 <= idx < len(texts)
                    or ordered[idx] is not None
                    or not isinstance(raw_vector, list) or not raw_vector
                ):
                    raise TypeError
                vector = [_safe_float(value) for value in raw_vector]
                dimension = len(vector) if dimension is None else dimension
                if len(vector) != dimension:
                    raise TypeError
                ordered[idx] = vector
            if any(v is None for v in ordered):
                raise TypeError
            if options.get("dimensions") and dimension != options["dimensions"]:
                raise TypeError
        except (KeyError, TypeError, IndexError):
            raise ProviderError("Invalid embeddings response") from None
        return EmbedResult(embeddings=[v for v in ordered if v is not None], model=model)

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[RerankEntry]:
        """Optional /reranks endpoint; Docker Model Runner does not advertise it."""
        if not model or not query or not documents or any(not d for d in documents):
            raise ValidationError("rerank requires model, query and documents")
        options = parameters or {}
        if set(options) - {"instruct"}:
            raise ValidationError("unsupported rerank parameters")
        data = self._request(
            "reranks",
            {
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                **options,
            },
        )
        try:
            results = data["results"]
            if not isinstance(results, list) or len(results) != len(documents):
                raise TypeError
            indices: set[int] = set()
            entries = []
            for value in results:
                idx = value["index"]
                if type(idx) is not int or not 0 <= idx < len(documents) or idx in indices:
                    raise TypeError
                indices.add(idx)
                entries.append(
                    RerankEntry(
                        index=idx,
                        score=_safe_float(value["relevance_score"]),
                        document=documents[idx],
                    )
                )
        except (KeyError, TypeError):
            raise ProviderError("Invalid rerank response") from None
        return sorted(entries, key=lambda item: (-item.score, item.index))

    def health(self) -> bool:
        """Not a health probe; explicit model readiness checks are still required."""
        return False
