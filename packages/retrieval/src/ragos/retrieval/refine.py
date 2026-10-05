"""The shared sentence-level refinement node.

All three relevance-gate outcomes pass through this one node before anything is
generated (architecture §8.4). The reason is not tidiness: three branches that
each assemble their own context is how the refusal path starts returning
half-answers while the proceed path looks fine, and the divergence is invisible
until a user hits it.

The node produces one question sentence. That sentence is what a re-retrieval
runs on the ``ambiguous`` path and what generation is answered against on
``proceed``. The ``refuse`` path still runs the node — its output is recorded in
the explain — so there is exactly one refinement behaviour to reason about.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.grader import GateVerdict
from ragos.retrieval.query.rewriter import CompleteFn
from ragos.retrieval.types import ScoredChunk

LOGGER = get_logger(__name__)

RefineFn = Callable[["RefineRequest", TenantContext], Awaitable[str]]

MAX_REFINE_CHARS = 1000

REFINE_SYSTEM_PROMPT = """\
Rewrite the question into one self-contained sentence that can be answered from a
single retrieved passage.

Rules:
- Spell out every reference that depends on earlier context.
- Keep the original language and the original intent.
- Output one sentence, no quotes, no numbering, no commentary.
"""

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCT = " \t\r\n。．.!?！？,，;；:："


@dataclass(frozen=True, slots=True)
class RefineRequest:
    """What the refine node is given.

    ``verdict`` is ``None`` when the gate is disabled, which is a distinct state
    from "graded as relevant": the node must not assume a grade exists just
    because it was handed a verdict field.
    """

    query: str
    verdict: str | None
    action: str
    evidence: tuple[ScoredChunk, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return self.verdict == GateVerdict.AMBIGUOUS


def deterministic_refine(query: str) -> str:
    """Normalize whitespace and trailing punctuation.

    A normalization, not a semantic rewrite — it cannot resolve a pronoun. It
    exists so the node still produces a usable sentence when no model is wired,
    rather than passing a multi-line question blob into a retriever.
    """
    collapsed = _WHITESPACE.sub(" ", query or "").strip()
    if not collapsed:
        return collapsed
    if len(collapsed) <= MAX_REFINE_CHARS:
        return collapsed.rstrip(_TRAILING_PUNCT)

    # Truncating mid-word produces a different question, so cut at the last
    # sentence or whitespace boundary that fits.
    window = collapsed[:MAX_REFINE_CHARS]
    for boundary in (". ", "。", "? ", "？", " "):
        cut = window.rfind(boundary)
        if cut > MAX_REFINE_CHARS // 2:
            return window[:cut].rstrip(_TRAILING_PUNCT)
    return window.rstrip(_TRAILING_PUNCT)


class SentenceRefiner:
    """The one refine node. Injected, never constructed per call site."""

    def __init__(self, complete: CompleteFn | None = None) -> None:
        self.complete = complete

    async def refine(self, request: RefineRequest, tenant_ctx: TenantContext) -> str:
        if self.complete is None:
            return deterministic_refine(request.query)
        try:
            raw = await self.complete(
                [
                    {"role": "system", "content": REFINE_SYSTEM_PROMPT},
                    {"role": "user", "content": request.query},
                ],
                tenant_ctx,
            )
        except Exception:
            LOGGER.exception("sentence refinement failed; using deterministic refinement")
            return deterministic_refine(request.query)

        candidate = (raw or "").strip()
        if not candidate:
            return deterministic_refine(request.query)
        return candidate[:MAX_REFINE_CHARS]
