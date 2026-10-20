"""Explicit local smoke probe for OpenAI-compatible model endpoints.

Example:
    RAGOS_PROBE_BASE_URL=http://localhost:12434/engines/v1 \\
    RAGOS_PROBE_CHAT_MODEL='ai/qwen3:4b-instruct-2507-q4_K_M' \\
    RAGOS_PROBE_EMBED_MODEL='ragos/qwen3-embedding:0.6b-q8' \\
    python scripts/check_model_api.py

Never sends tenant content or reads the encrypted platform key pool.
Rerank is optional and only probed when its API/model is explicitly configured.
"""

from __future__ import annotations

import os
import sys

from ragos.core.errors import RagOSError
from ragos.model_gateway.providers.openai_compatible import OpenAICompatibleProvider


def main() -> int:
    base_url = os.getenv("RAGOS_PROBE_BASE_URL", "")
    chat_name = os.getenv("RAGOS_PROBE_CHAT_MODEL", "")
    embed_name = os.getenv("RAGOS_PROBE_EMBED_MODEL", "")
    rerank_name = os.getenv("RAGOS_PROBE_RERANK_MODEL", "")
    if not base_url or not chat_name or not embed_name:
        print(
            "Configure RAGOS_PROBE_BASE_URL, RAGOS_PROBE_CHAT_MODEL "
            "and RAGOS_PROBE_EMBED_MODEL.",
            file=sys.stderr,
        )
        return 2

    try:
        provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=os.getenv("RAGOS_PROBE_API_KEY") or None,
        )
        chat = provider.chat(
            messages=[{"role": "user", "content": "Reply with a short greeting."}],
            model=chat_name,
        )
        embeddings = provider.embed(texts=["document page example"], model=embed_name)
        if not chat.content.strip() or not embeddings.embeddings[0]:
            print("FAIL: model returned empty content or embedding", file=sys.stderr)
            return 1
        print(f"OK chat model={chat.model}, response_chars={len(chat.content)}")
        print(
            f"OK embed model={embeddings.model}, "
            f"dimension={len(embeddings.embeddings[0])}"
        )
        if rerank_name:
            entries = provider.rerank(
                query="Which page says warranty?",
                documents=["The warranty is 24 months.", "Invoice total is $50."],
                model=rerank_name,
            )
            print(f"OK rerank model={rerank_name}, scores={len(entries)}")
        else:
            print("SKIP rerank: no RAGOS_PROBE_RERANK_MODEL configured")
    except RagOSError as exc:
        print(f"FAIL: {exc.code} (details suppressed)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
