"""Run an opt-in, local-only Qwen Chat + Embedding + real PDF RAG smoke.

This CLI generates ONLY synthetic contract PDF bytes, extracts via Poppler,
queries the operator's local model endpoint and records a content-free JSON
report. Never use real customer PDFs; this is not the production ModelRouter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from qdrant_client import QdrantClient
from ragos.core.errors import ValidationError
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos.runtime.local_pdf_model_probe import run_synthetic_pdf_model_probe
from ragos.storage.repositories.search import SearchTier
from ragos.storage.vector import collection_name

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "model-runner.docker.internal"}
_QDRANT_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _check_local_url(url: str, *, qdrant: bool = False) -> None:
    parsed = urlsplit(url)
    hosts = _QDRANT_HOSTS if qdrant else _LOCAL_HOSTS
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (qdrant and parsed.path not in {"", "/"})
    ):
        raise ValidationError("local model/Qdrant smoke forbids remote or credentialed URLs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic PDF and check local Qwen Embedding+Chat RAG"
    )
    parser.add_argument("--output", type=Path, help="write content-free JSON report")
    args = parser.parse_args(argv)
    base_url = os.getenv("RAGOS_PROBE_BASE_URL", "")
    chat_model = os.getenv("RAGOS_PROBE_CHAT_MODEL", "")
    embed_model = os.getenv("RAGOS_PROBE_EMBED_MODEL", "")
    revision = os.getenv("RAGOS_PROBE_EMBED_REVISION", "")
    dimension = os.getenv("RAGOS_PROBE_EMBED_DIM", "")
    qdrant_url = os.getenv("RAGOS_PROBE_QDRANT_URL", "")
    if not all((base_url, chat_model, embed_model, revision, dimension)):
        print(
            "Set RAGOS_PROBE_BASE_URL, RAGOS_PROBE_CHAT_MODEL, "
            "RAGOS_PROBE_EMBED_MODEL, RAGOS_PROBE_EMBED_REVISION "
            "and RAGOS_PROBE_EMBED_DIM",
            file=sys.stderr,
        )
        return 2
    try:
        _check_local_url(base_url)
        if qdrant_url:
            _check_local_url(qdrant_url, qdrant=True)
        spec = FrozenEmbeddingSpec(
            model=embed_model,
            model_digest=revision,
            dimension=int(dimension),
        )
        provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=os.getenv("RAGOS_PROBE_API_KEY") or None,
            timeout_seconds=60,
        )
        if not qdrant_url:
            report = run_synthetic_pdf_model_probe(
                provider, embedding_spec=spec, chat_model=chat_model
            )
        else:
            # Dedicated random namespace per run, with explicit cleanup.
            # Refuse existing collections; NEVER overwrite or erase user data.
            prefix = "ragos_probe_" + uuid4().hex[:16]
            owned_collection = collection_name(
                embedding_profile=spec.profile,
                embedding_version=spec.version,
                tier=SearchTier.STABLE,
                prefix=prefix,
            )
            with QdrantClient(url=qdrant_url, timeout=10) as client:
                if any(
                    item.name == owned_collection
                    for item in client.get_collections().collections
                ):
                    raise ValidationError("local Qdrant scratch collection already exists")
                try:
                    report = run_synthetic_pdf_model_probe(
                        provider, embedding_spec=spec, chat_model=chat_model,
                        client=client, collection_prefix=prefix,
                    )
                finally:
                    # Delete only the exact randomly namespaced scratch
                    # collection from this attempt, including failed attempts.
                    if any(
                        item.name == owned_collection
                        for item in client.get_collections().collections
                    ):
                        client.delete_collection(collection_name=owned_collection)

        encoded = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            args.output.write_text(encoded, encoding="utf-8")
            print("Local synthetic PDF smoke report written (no source/answer text).")
        return 0
    except Exception:
        # Endpoint and model names can be private; never print provider bodies,
        # secrets, prompt text, or paths on failure.
        print(
            "Local PDF+Qwen smoke failed; check the local model, vector "
            "dimension, Poppler and endpoint",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
