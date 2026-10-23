"""Contract and HTTP-wire tests for the explicit S1 model API adapter.

All responses are fixtures served by a local test transport; CI makes no
paid model calls and cannot claim real model compatibility.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from ragos.core.errors import ProviderError, ValidationError
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider


class FixtureTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _provider(response, *, url="https://example.invalid/v1", secret="test-secret"):
    transport = FixtureTransport(response)
    return OpenAICompatibleProvider(
        base_url=url, api_key=secret, timeout_seconds=5, transport=transport
    ), transport


def test_chat_calls_real_protocol_route_and_parses_content() -> None:
    provider, transport = _provider(
        {
            "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }
    )
    response = provider.chat(
        model="qwen",
        messages=[{"role": "user", "content": "Hello?"}],
        parameters={"temperature": 0.2},
    )

    assert response.content == "Hello"
    assert response.usage["prompt_tokens"] == 2
    url, body, headers, timeout = transport.calls[0]
    assert url == "https://example.invalid/v1/chat/completions"
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert body["model"] == "qwen"
    assert headers["Authorization"] == "Bearer test-secret"
    assert timeout == 5


def test_chat_accepts_real_qwen_usage_with_nested_details() -> None:
    response = {
        "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
        "usage": {
            "completion_tokens": 1,
            "prompt_tokens": 2,
            "total_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    provider, transport = _provider(response)
    chunk = provider.chat(messages=[{"role": "user", "content": "Hi?"}], model="qwen")

    assert chunk.content == "Hi"
    assert chunk.usage == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    # The provider response dict (and its usage) must not be mutated.
    assert transport.response["usage"] == {
        "completion_tokens": 1,
        "prompt_tokens": 2,
        "total_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


@pytest.mark.parametrize(
    "details",
    [None, {"cached_tokens": 0}, {"cached_tokens": 4, "reasoning_tokens": 2}],
)
def test_chat_accepts_null_or_nonnegative_integer_usage_details(details) -> None:
    provider, _ = _provider(
        {
            "choices": [{"message": {"content": "Hi"}}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "prompt_tokens_details": details,
                "completion_tokens_details": details,
            },
        }
    )
    chunk = provider.chat(messages=[{"role": "user", "content": "Hi?"}], model="qwen")

    # Standard detail objects are validated then stripped from internal usage.
    assert chunk.usage == {"prompt_tokens": 2, "completion_tokens": 1}


@pytest.mark.parametrize(
    "bad_usage",
    [
        {"prompt_tokens": 2, "prompt_tokens_details": 5},
        {"prompt_tokens": 2, "prompt_tokens_details": "3"},
        {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": -1}},
        {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": True}},
        {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": {"x": 1}}},
        {"prompt_tokens": 2, "custom_details": {"anything": 1}},
        {"completion_tokens": True},
        {"total_tokens": -1},
        {"completion_tokens": 1.0},
        {"prompt_tokens": 2, "unknown_extra": "3"},
    ],
)
def test_chat_rejects_invalid_usage_shapes(bad_usage) -> None:
    response = {
        "choices": [{"message": {"content": "Hi"}}],
        "usage": dict(bad_usage),
    }
    provider, transport = _provider(response)
    with pytest.raises(ProviderError, match="Invalid chat completion response"):
        provider.chat(messages=[{"role": "user", "content": "x"}], model="q")
    assert transport.response["usage"] == bad_usage


def test_chat_without_usage_reports_none() -> None:
    provider, _ = _provider({"choices": [{"message": {"content": "Hi"}}]})
    chunk = provider.chat(messages=[{"role": "user", "content": "x"}], model="q")
    assert chunk.usage is None


def test_embedding_indexes_are_restored_to_input_order() -> None:
    provider, transport = _provider(
        {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }
    )
    result = provider.embed(texts=["page seven", "page eight"], model="embedding")

    assert result.embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert transport.calls[0][0].endswith("/embeddings")
    assert transport.calls[0][1]["encoding_format"] == "float"


@pytest.mark.parametrize(
    "data",
    [
        {"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
        {"data": [
            {"index": 0, "embedding": [1.0]},
            {"index": 0, "embedding": [1.0]},
        ]},
        {"data": [
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [1.0, 2.0]},
        ]},
        {"data": [
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [float("nan")]},
        ]},
    ],
)
def test_invalid_embedding_responses_fail_closed(data) -> None:
    provider, _ = _provider(data)
    with pytest.raises(ProviderError):
        provider.embed(texts=["a", "b"], model="embedding")


def test_rerank_scores_follow_explicit_indices() -> None:
    provider, transport = _provider(
        {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.94},
            ]
        }
    )
    ranked = provider.rerank(query="warranty", documents=["24 months", "invoice"], model="qwen")

    assert [(item.index, item.score, item.document) for item in ranked] == [
        (0, 0.94, "24 months"),
        (1, 0.2, "invoice"),
    ]
    assert transport.calls[0][0].endswith("/reranks")
    assert transport.calls[0][1]["top_n"] == 2


def test_missing_or_partial_rerank_scores_are_not_fake_success() -> None:
    provider, _ = _provider({"results": [{"index": 0, "relevance_score": 0.8}]})
    with pytest.raises(ProviderError, match="Invalid rerank response"):
        provider.rerank(query="x", documents=["a", "b"], model="qwen")


def test_overflowing_numeric_provider_responses_are_rejected() -> None:
    provider, _ = _provider({
        "results": [{"index": 0, "relevance_score": 10**1000}]
    })
    with pytest.raises(ProviderError, match="invalid numeric"):
        provider.rerank(query="warranty", documents=["document"], model="qwen")


def test_unsupported_provider_api_is_an_error_not_zero_scores() -> None:
    provider, _ = _provider(ProviderError("Model provider request failed"))
    with pytest.raises(ProviderError):
        provider.rerank(query="x", documents=["a"], model="unsupported")


def test_reject_untrusted_plaintext_and_base_url_credentials() -> None:
    for url in [
        "http://example.com/v1",
        "http://localhost:12434/engines/v1?token=secret",
        "https://username:password@example.com/v1",
        "file:///tmp/api/v1",
        "https://example.com/api",
    ]:
        with pytest.raises(ValidationError):
            OpenAICompatibleProvider(base_url=url)


def test_local_model_runner_url_is_allowed_and_key_is_optional() -> None:
    provider, transport = _provider(
        {"choices": [{"message": {"content": "OK"}}]},
        url="http://localhost:12434/engines/v1",
        secret="",
    )
    assert provider.chat(messages=[{"role": "user", "content": "x"}], model="q").content == "OK"
    assert transport.calls[0][0] == (
        "http://localhost:12434/engines/v1/chat/completions"
    )
    assert "Authorization" not in transport.calls[0][2]


def test_sensitive_transport_exception_is_not_echoed() -> None:
    provider, _ = _provider(RuntimeError("key=secret-and-document-text"))
    with pytest.raises(ProviderError) as exc:
        provider.chat(messages=[{"role": "user", "content": "private"}], model="q")
    assert "secret" not in str(exc.value)
    assert "document" not in str(exc.value)


def test_unknown_parameters_and_empty_requests_refuse_before_transport() -> None:
    provider, transport = _provider({})
    with pytest.raises(ValidationError):
        provider.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="q",
            parameters={"token": "sensitive"},
        )
    with pytest.raises(ValidationError):
        provider.embed(texts=[], model="embed")
    assert not transport.calls


def test_invalid_chat_response_is_not_success() -> None:
    provider, _ = _provider({"choices": [{"message": {"content": None}}]})
    with pytest.raises(ProviderError):
        provider.chat(messages=[{"role": "user", "content": "x"}], model="q")


def test_health_is_not_falsely_reported_as_verified() -> None:
    provider, _ = _provider({})
    assert provider.health() is False


def test_actual_local_http_post_wire_format_and_opaque_error() -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            requests.append((self.path, json.loads(self.rfile.read(size))))
            if self.path.endswith("/reranks"):
                self.send_response(404)
                payload = {"message": "secret private upstream error"}
            else:
                self.send_response(200)
                payload = {
                    "choices": [{"message": {"content": "hello from local server"}}]
                }
            raw = json.dumps(payload).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/v1"
        )
        message = provider.chat(messages=[{"role": "user", "content": "test"}], model="q")
        assert message.content == "hello from local server"
        assert requests[0][0] == "/v1/chat/completions"
        with pytest.raises(ProviderError) as exc:
            provider.rerank(query="x", documents=["a"], model="q")
        assert "private upstream error" not in str(exc.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
