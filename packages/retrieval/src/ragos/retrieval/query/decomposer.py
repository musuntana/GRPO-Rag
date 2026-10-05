"""Query decomposition.

A multi-part question ("how do I rotate keys, and what does rotation cost?")
scores each part badly against a single vector: the average of two intents
matches neither document set well. Splitting it and retrieving per part recovers
both, at the cost of N round-trips and a merge that has to be honest about which
channel produced which hit.

The split is capped. An unbounded decomposition turns one user question into a
fan-out that costs more than the question was worth, and the cap is a recorded
parameter rather than a constant chosen here.
"""

from __future__ import annotations

import json
import re

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.query.rewriter import CompleteFn

LOGGER = get_logger(__name__)

DECOMPOSE_SYSTEM_PROMPT = """\
Split a multi-part question into independent search queries.

Rules:
- One question per line, no numbering, no commentary.
- Each line must be answerable on its own, with all needed context spelled out.
- Do not split a question that is already a single intent.
- Return at most four lines. If the question is single-intent, return it unchanged.
"""

DEFAULT_MAX_SUB_QUERIES = 4

#: Connectives that mark a genuine second intent. Deliberately conservative: a
#: split on a comma would shred a well-formed single question into fragments
#: that retrieve nothing.
_SPLIT_PATTERN = re.compile(
    r"\s+(?:and also|and then|as well as|and)\s+"
    r"|\s*(?:以及|并且|同时|还有|另外)\s*",
    re.IGNORECASE,
)


class Decomposer:
    """Split a multi-intent question into independently retrievable queries."""

    def __init__(
        self,
        complete: CompleteFn | None = None,
        *,
        max_sub_queries: int = DEFAULT_MAX_SUB_QUERIES,
    ) -> None:
        self.complete = complete
        self.max_sub_queries = max_sub_queries

    async def decompose(self, query: str, tenant_ctx: TenantContext) -> tuple[str, ...]:
        if self.complete is not None:
            subs = await self._decompose_with_llm(query, tenant_ctx)
            if subs:
                return subs
        return self._decompose_deterministically(query)

    async def _decompose_with_llm(
        self, query: str, tenant_ctx: TenantContext
    ) -> tuple[str, ...]:
        try:
            raw = await self.complete(
                [
                    {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                tenant_ctx,
            )
        except Exception:
            LOGGER.exception("query decomposition failed; using deterministic fallback")
            return ()
        lines = [
            line.strip(" -•\t")
            for line in (raw or "").splitlines()
            if line.strip(" -•\t")
        ]
        return self._finalize(lines, query)

    def _decompose_deterministically(self, query: str) -> tuple[str, ...]:
        parts = [p.strip(" ?？") for p in _SPLIT_PATTERN.split(query) if p and p.strip(" ?？")]
        if len(parts) <= 1:
            return (query,)
        return self._finalize(parts, query)

    def _finalize(self, candidates: list[str], query: str) -> tuple[str, ...]:
        """Dedupe, cap, and guarantee a non-empty result.

        A decomposition that returns nothing leaves the pipeline with no query to
        retrieve with, so the original question is always retained as a floor.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(candidate)
        if not ordered:
            return (query,)
        if len(ordered) > self.max_sub_queries:
            LOGGER.info(
                "decomposition exceeded the sub-query cap; truncating",
                extra={"produced": len(ordered), "cap": self.max_sub_queries},
            )
            ordered = ordered[: self.max_sub_queries]
        return tuple(ordered)


def parse_decomposition(raw: str) -> list[str]:
    """Parse a JSON or line-list decomposition response.

    Accepts both shapes because a model asked for plain lines frequently wraps
    them in a JSON object anyway, and rejecting that wastes a call.
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            values = payload.get("sub_queries") or payload.get("queries") or []
        elif isinstance(payload, list):
            values = payload
        else:
            values = []
        return [str(v).strip() for v in values if str(v).strip()]
    return [line.strip(" -•\t") for line in text.splitlines() if line.strip(" -•\t")]
