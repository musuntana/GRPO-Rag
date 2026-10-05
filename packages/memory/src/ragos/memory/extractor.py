"""Fact extraction.

Runs after a conversation, never inside it (AGENTS.md memory rule 3). The model is
asked for *slots* as well as statements, because a fact that contradicts an
earlier one can only be recognised as a contradiction if something has named what
it is about — free text alone makes "I moved to Berlin" and "I live in Berlin"
indistinguishable from a contradiction.

Two parse decisions worth stating, because both are the difference between a
memory system that is quietly wrong and one that is merely lossy:

- a **malformed record is dropped, not guessed**. Recovering a fact's text from a
  broken JSON object requires inventing content, and an invented preference is a
  confidently wrong answer later.
- an **unknown category is kept at low confidence** rather than discarded. Losing
  a stated preference costs recall; mis-filing one costs routing. The asymmetry
  decides it: a category only changes which channel's budget the fact lands in.
"""

from __future__ import annotations

import json

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.memory.types import (
    CompleteFn,
    ExtractedFact,
    FactCategory,
    FactOperation,
)

LOGGER = get_logger(__name__)

EXTRACTION_SYSTEM_PROMPT = """\
Extract durable facts about the user from the conversation.

For each fact return:
- subject_key: a short stable slot name, lower_snake_case, e.g. "city_of_residence",
  "report_format_preference", "primary_project". Use the SAME slot name every time
  the same kind of fact is stated, so a change can be recognised as a change.
- fact: one sentence stating the fact.
- category: preference, behavior, context, or entity.
- confidence: 0 to 1.
- operation: "add", or "replace" when this updates a slot you have stated before.

Rules:
- Only facts about the user, their work, or entities they named.
- No inferences, no summaries of the conversation, no questions.
- Nothing time-bound ("today", "this week") unless it is a stated preference.
- Return strict JSON: {"facts": [{"subject_key": ..., "fact": ..., "category": ...,
  "confidence": ..., "operation": ...}]}
"""

#: A category the model invented is filed as ``context`` at reduced confidence.
_FALLBACK_CATEGORY = FactCategory.CONTEXT
_UNCATEGORIZED_CONFIDENCE = 0.3


class FactExtractor:
    def __init__(self, complete: CompleteFn) -> None:
        self.complete = complete

    async def extract(
        self,
        transcript: list[dict],
        *,
        tenant_ctx: TenantContext,
    ) -> list[ExtractedFact]:
        payload = json.dumps({"transcript": transcript}, ensure_ascii=False)
        raw = await self.complete(
            [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            tenant_ctx,
        )
        return parse_extraction(raw)


def parse_extraction(raw: str | None) -> list[ExtractedFact]:
    """Parse the model's response into facts, dropping anything malformed."""
    try:
        payload = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        LOGGER.warning("fact extraction returned unparseable output; storing nothing")
        return []

    entries = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    facts: list[ExtractedFact] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("fact", "")).strip()
        if not text:
            continue

        category = _parse_category(entry.get("category"))
        operation = _parse_operation(entry.get("operation"))
        subject_key = str(entry.get("subject_key", "")).strip().lower()[:128]

        if operation == FactOperation.REPLACE and not subject_key:
            LOGGER.info("dropping a replace with no subject_key; there is nothing to close")
            continue

        facts.append(
            ExtractedFact(
                fact=text,
                category=category or _FALLBACK_CATEGORY,
                subject_key=subject_key,
                confidence=_parse_confidence(
                    entry.get("confidence"), known=category is not None
                ),
                operation=operation,
            )
        )
    return facts


def _parse_category(value: object) -> FactCategory | None:
    """``None`` marks a category the model invented."""
    try:
        return FactCategory(str(value).strip().lower())
    except ValueError:
        LOGGER.info("unknown fact category; filing as context at reduced confidence")
        return None


def _parse_operation(value: object) -> FactOperation:
    try:
        return FactOperation(str(value).strip().lower())
    except ValueError:
        return FactOperation.ADD


def _parse_confidence(value: object, *, known: bool) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5 if known else _UNCATEGORIZED_CONFIDENCE
    score = max(0.0, min(1.0, score))
    # A confident claim in a category the model invented is capped: the claim may
    # be true while our reading of what kind of claim it is is not.
    return score if known else min(score, _UNCATEGORIZED_CONFIDENCE)
