"""CLI failure regression for the frozen Qwen local embedding smoke.

The operator CLI owns an explicit in-memory Qdrant client and must close it in a
``finally`` block. The installed ``qdrant-client`` release does not implement the
context-manager protocol on ``QdrantClient``, so ``with QdrantClient(":memory:")``
raises ``TypeError`` before the probe can run.

These tests use the REAL ``QdrantClient`` (in-memory local mode) and mock only the
OpenAI-compatible model HTTP transport, so a context-manager capable fake client
cannot mask the regression.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient
from ragos.model_gateway.providers import openai_compatible

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_frozen_embedding.py"
MODEL = "qwen3_embedding_smoke"
DIGEST = "a" * 12
BASE_URL = "http://127.0.0.1:12434/v1"


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("check_frozen_embedding", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEmbeddingTransport:
    """Mock ONLY the model HTTP boundary (/embeddings); never the vector store."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, url: str, payload: dict[str, Any], headers: Any, timeout: float) -> Any:
        assert url.endswith("/embeddings")
        assert payload["encoding_format"] == "float"
        texts: list[str] = payload["input"]
        self.calls.extend(texts)
        if self.fail:
            raise RuntimeError("local model endpoint unavailable")
        vectors = [
            [1.0, 0.0, 0.0] if "warranty" in text.lower() else [0.0, 1.0, 0.0] for text in texts
        ]
        return {"data": [{"index": i, "embedding": vector} for i, vector in enumerate(vectors)]}


def _prepare_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, FakeEmbeddingTransport, list[Any], list[QdrantClient]]:
    cli = _load_cli()
    monkeypatch.setenv("RAGOS_PROBE_BASE_URL", BASE_URL)
    monkeypatch.setenv("RAGOS_PROBE_EMBED_MODEL", MODEL)
    monkeypatch.setenv("RAGOS_PROBE_EMBED_REVISION", DIGEST)
    monkeypatch.setenv("RAGOS_PROBE_EMBED_DIM", "3")
    transport = FakeEmbeddingTransport()
    monkeypatch.setattr(openai_compatible, "_http_json", transport)

    created: list[Any] = []
    real_client_cls = cli.QdrantClient

    def factory(*args: Any, **kwargs: Any) -> QdrantClient:
        client = real_client_cls(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(cli, "QdrantClient", factory)

    closed: list[QdrantClient] = []
    real_close = QdrantClient.close

    def tracking_close(self: QdrantClient, grpc_grace: float | None = None, **kwargs: Any) -> None:
        closed.append(self)
        return real_close(self, grpc_grace=grpc_grace, **kwargs)

    monkeypatch.setattr(QdrantClient, "close", tracking_close)
    return cli, transport, created, closed


def test_frozen_embedding_cli_closes_real_memory_client_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli, transport, created, closed = _prepare_cli(monkeypatch)
    assert cli.main() == 0
    assert transport.calls
    assert len(created) == 1
    assert closed == created
    assert "OK model_revision=" in capsys.readouterr().out


def test_frozen_embedding_cli_closes_real_memory_client_on_model_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli, transport, created, closed = _prepare_cli(monkeypatch)
    # The transport mock fails the model HTTP boundary; vector store must still close.
    monkeypatch.setattr(transport, "fail", True)
    assert cli.main() == 1
    assert created
    assert closed == created
    assert "no source leaked" in capsys.readouterr().out
