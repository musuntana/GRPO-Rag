"""HyDE — Hypothetical Document Embedding.

The observation: a question and a document that answers it live in different
parts of the embedding space. Embedding a plausible answer instead of the
question moves the query vector toward the documents that would satisfy it, which
helps most on short or vague questions and is roughly neutral on long specific
ones. That asymmetry is why it is a profile-selected strategy rather than the
default (architecture §8.2).

Two failure modes are handled explicitly, because both are silent:

- an empty hypothetical would be embedded as an empty string, which lands at an
  arbitrary point in the space and returns confident nonsense
- a hypothetical that merely restates the question adds a call without moving the
  vector, so it is reported rather than counted as a strategy success
"""

from __future__ import annotations

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.query.rewriter import CompleteFn

LOGGER = get_logger(__name__)

HYDE_SYSTEM_PROMPT = """\
Write a short factual passage that would answer the question.

Rules:
- Write the passage, not the question. Two to four sentences.
- Include the specific nouns, identifiers, and units a real document would use.
- Do not hedge, do not ask questions, do not add commentary.
- If the question cannot be answered from any document, write nothing.
"""

MAX_HYPOTHETICAL_CHARS = 1200


class HyDE:
    """Generate the hypothetical answer document for a query."""

    def __init__(self, complete: CompleteFn | None = None) -> None:
        self.complete = complete

    async def generate(self, query: str, tenant_ctx: TenantContext) -> str | None:
        if self.complete is None:
            LOGGER.warning("hyde requested without a completion callable; skipping")
            return None
        try:
            raw = await self.complete(
                [
                    {"role": "system", "content": HYDE_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                tenant_ctx,
            )
        except Exception:
            LOGGER.exception("hyde generation failed; falling back to the raw query")
            return None

        passage = (raw or "").strip()
        if not passage:
            LOGGER.info("hyde produced no passage; falling back to the raw query")
            return None
        return passage[:MAX_HYPOTHETICAL_CHARS]


def is_informative(hypothetical: str, query: str) -> bool:
    """Whether the hypothetical adds anything over the question itself.

    Reported so a profile that pays for HyDE on every query can be compared
    against one that pays for it and gets the question back.
    """
    normalized = hypothetical.strip().lower()
    if not normalized:
        return False
    question_tokens = {t for t in query.lower().split() if len(t) > 1}
    if not question_tokens:
        return True
    overlap = sum(1 for t in question_tokens if t in normalized)
    return overlap < len(question_tokens)
