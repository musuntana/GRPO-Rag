"""The relevance rails grader.

One grader, two consumers: the production CRAG gate and the evaluation harness.
That is what makes a gate change always ship with a recorded metric delta
(architecture §8.4, §13.3).

The prompt lives in one versioned object. It was previously specified as a
concept in two places and written down in neither, which is how a prompt that
governs both production and evaluation ends up unversioned — the failure this
module exists to close.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from ragos.core.errors import ProviderError
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import CragThresholds, ScoredChunk

RAILS_PROMPT_VERSION = "rails-v1"

#: One prompt, both consumers. The grade scale is deliberately the same scale
#: the eval set uses for ``GoldEvidence.grade`` — that shared vocabulary is what
#: lets NDCG@k be computed from gate output without a translation step that could
#: itself drift.
RAILS_SYSTEM_PROMPT = """\
You grade retrieved passages for a question. For each passage return a relevance
grade from 0 to 3:

0 — irrelevant: different topic, does not help answer the question
1 — related but not answering: same document or subject area, does not contain
    the information needed
2 — supporting: contains information that helps answer part of the question
3 — answering: contains the information needed to answer the question

Return strict JSON: {"grades": [{"chunk_id": "<id>", "grade": <0-3>}]}
Return nothing else.
"""


@dataclass(slots=True)
class GradedChunk:
    chunk_id: UUID
    doc_id: UUID | None
    grade: int
    score: float
    page_no: int | None = None


def rails_prompt_id(system_prompt: str = RAILS_SYSTEM_PROMPT) -> str:
    """Content-addressed prompt id, so a prompt edit is a visible version bump."""
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    return f"{RAILS_PROMPT_VERSION}-{digest}"


def build_rails_request(query: str, chunks: list[ScoredChunk]) -> list[dict]:
    """Render the passages a judge sees.

    Page number is included when present because a judge grading a table is
    often deciding between two pages of the same document, and without the page
    label those passages are indistinguishable.
    """
    passages = []
    for idx, chunk in enumerate(chunks, start=1):
        label = f"[{idx}]"
        if chunk.page_no is not None:
            label += f" (page {chunk.page_no})"
        passages.append(
            {
                "marker": label,
                "chunk_id": str(chunk.chunk_id),
                "text": chunk.text,
            }
        )
    user_payload = json.dumps(
        {"question": query, "passages": passages}, ensure_ascii=False
    )
    return [
        {"role": "system", "content": RAILS_SYSTEM_PROMPT},
        {"role": "user", "content": user_payload},
    ]


def parse_rails_response(raw: str, chunks: list[ScoredChunk]) -> list[GradedChunk]:
    """Require a complete, unambiguous grade for every supplied candidate.

    A protocol failure is a provider error, not a relevance observation. Never
    coerce, clamp or fill missing grades to turn an outage into normal abstention.
    """
    by_id = {str(c.chunk_id): c for c in chunks}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, RecursionError):
        raise ProviderError("invalid judge response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("grades"), list):
        raise ProviderError("invalid judge response")

    grades: list[GradedChunk] = []
    seen: set[str] = set()
    for entry in payload["grades"]:
        if not isinstance(entry, dict):
            raise ProviderError("invalid judge response")
        chunk_id = entry.get("chunk_id")
        grade = entry.get("grade")
        if (
            not isinstance(chunk_id, str)
            or chunk_id not in by_id
            or chunk_id in seen
            or type(grade) is not int
            or not 0 <= grade <= 3
        ):
            raise ProviderError("invalid judge response")
        chunk = by_id[chunk_id]
        seen.add(chunk_id)
        grades.append(GradedChunk(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            grade=grade,
            score=chunk.score,
            page_no=chunk.page_no,
        ))
    if seen != set(by_id):
        raise ProviderError("incomplete judge response")
    return grades


class GateVerdict:
    """``relevant`` / ``ambiguous`` / ``irrelevant`` from graded chunks.

    A confidence is derived from the grades rather than asked for separately, so
    the number the thresholds are compared against is the number the eval set
    also records.
    """

    RELEVANT = "relevant"
    AMBIGUOUS = "ambiguous"
    IRRELEVANT = "irrelevant"

    ALL = (RELEVANT, AMBIGUOUS, IRRELEVANT)


def classify(grades: list[GradedChunk], thresholds: CragThresholds) -> tuple[str, float]:
    """Map grades onto a gate verdict using versioned thresholds.

    Thresholds are explicit rather than implicit in the prompt: an implicit
    threshold is a constant nobody can evaluate, and design principle 9 makes
    relevance thresholds an evaluated parameter.
    """
    if not grades:
        return GateVerdict.IRRELEVANT, 0.0

    best = max(grades, key=lambda g: (g.grade, g.score))
    confidence = best.grade / 3.0

    if confidence >= thresholds.relevant_at:
        return GateVerdict.RELEVANT, confidence
    if all(g.grade / 3.0 < thresholds.irrelevant_below for g in grades):
        return GateVerdict.IRRELEVANT, confidence
    return GateVerdict.AMBIGUOUS, confidence


#: Signature of the judge call. Injected so the gate can run against a provider
#: in production and a canned response in tests. The tenant context is a
#: parameter rather than ambient state so the judge call is metered and
#: budget-checked like every other model call in the chain.
JudgeFn = Callable[[str, list[dict], TenantContext], Awaitable[str]]


@dataclass(slots=True)
class RelevanceGrader:
    """CRAG relevance gate over the reranked set."""

    judge: JudgeFn
    thresholds: CragThresholds
    prompt_id: str = RAILS_PROMPT_VERSION

    async def grade(
        self, query: str, chunks: list[ScoredChunk], tenant_ctx: TenantContext
    ) -> GateResult:
        if not chunks:
            return GateResult(
                verdict=GateVerdict.IRRELEVANT,
                confidence=0.0,
                action=GateAction.REFUSE,
                grades=(),
                prompt_id=self.prompt_id,
            )
        raw = await self.judge(query, build_rails_request(query, chunks), tenant_ctx)
        grades = parse_rails_response(raw, chunks)
        verdict, confidence = classify(grades, self.thresholds)
        return GateResult(
            verdict=verdict,
            confidence=confidence,
            action=_action_for(verdict),
            grades=tuple(grades),
            prompt_id=self.prompt_id,
        )


class GateAction:
    PROCEED = "proceed"
    RE_RETRIEVE = "re_retrieve"
    REFUSE = "refuse"

    ALL = (PROCEED, RE_RETRIEVE, REFUSE)


@dataclass(slots=True)
class GateResult:
    verdict: str
    confidence: float
    action: str
    grades: tuple[GradedChunk, ...]
    prompt_id: str


def _action_for(verdict: str) -> str:
    if verdict == GateVerdict.RELEVANT:
        return GateAction.PROCEED
    if verdict == GateVerdict.AMBIGUOUS:
        return GateAction.RE_RETRIEVE
    return GateAction.REFUSE