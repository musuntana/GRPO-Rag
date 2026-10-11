"""Model-gateway adapters for the retrieval chain.

Every LLM call the pipeline makes — grading, generation, rewriting, HyDE,
decomposition, refinement — goes through :class:`~ragos.model_gateway.router.ModelRouter`.
There is deliberately no path from a retrieval feature to a provider client:
entitlement checks belong behind the gateway. Budget pre-authorization, secret
resolution and cost attribution are required before real provider activation;
they are not yet implemented. Unconnected providers are refused, rather than
claiming that a gateway call alone proves those controls (guardrail 9).

The router's ``chat`` is synchronous, so each adapter runs it on a worker
thread. Blocking the event loop here would stall every concurrent request on the
process, which is the kind of defect that only appears under load.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from ragos.core.errors import ProviderError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import ScoredChunk


class RouterLike(Protocol):
    """The part of ``ModelRouter`` these adapters use."""

    def chat(
        self,
        tenant_ctx: TenantContext,
        *,
        messages: list[dict[str, Any]],
        use_case: str = "chat",
        profile_override: Any | None = None,
    ) -> Any: ...

    def embed(
        self,
        tenant_ctx: TenantContext,
        *,
        texts: list[str],
        use_case: str = "embed",
        profile_override: Any | None = None,
    ) -> Any: ...


class GatewayComplete:
    """``CompleteFn``: chat messages in, text out, billed to a tenant.

    Rewriting, HyDE, decomposition, and refinement all land here. They are
    per-request LLM calls that a naive implementation treats as free string
    manipulation, which is exactly how a chain ends up spending untracked tokens
    on query planning.
    """

    def __init__(self, router: RouterLike, *, use_case: str = "chat") -> None:
        self.router = router
        self.use_case = use_case

    async def __call__(self, messages: list[dict], tenant_ctx: TenantContext) -> str:
        chunk = await asyncio.to_thread(
            lambda: self.router.chat(
                tenant_ctx, messages=messages, use_case=self.use_case
            )
        )
        return chunk.content or ""


class GatewayGenerator:
    """``GenerateFn``: renders the grounded prompt and returns the answer.

    The evidence is numbered in the order the model sees it, and the numbering is
    what the citation markers index. Re-ordering or filtering evidence after the
    call would silently invalidate every marker.
    """

    def __init__(
        self,
        router: RouterLike,
        *,
        use_case: str = "chat",
        system_prompt: str | None = None,
        max_evidence_chars: int = 12000,
    ) -> None:
        if (
            isinstance(max_evidence_chars, bool)
            or not isinstance(max_evidence_chars, int)
            or max_evidence_chars < 1
        ):
            raise ValidationError("Evidence character budget must be a positive integer")
        self.router = router
        self.use_case = use_case
        self.system_prompt = system_prompt or GENERATION_SYSTEM_PROMPT
        self.max_evidence_chars = max_evidence_chars

    async def __call__(
        self,
        question: str,
        evidence: list[ScoredChunk],
        tenant_ctx: TenantContext,
    ) -> str:
        if sum(len(chunk.text) for chunk in evidence) > self.max_evidence_chars:
            # Dropping or truncating passages here would change delivered citation identity.
            raise ValidationError("Selected evidence exceeds the generation character budget")
        messages = build_generation_messages(
            question, evidence, system_prompt=self.system_prompt
        )
        chunk = await asyncio.to_thread(
            lambda: self.router.chat(
                tenant_ctx, messages=messages, use_case=self.use_case
            )
        )
        return chunk.content or ""


class GatewayEmbedder:
    """``EmbedFn``: the query vector for dense recall.

    A retrieval read is a model call like any other — it is metered, it can be
    budget-blocked, and it can fail. Routing it through the gateway is what keeps
    dense recall from becoming an untracked cost line.
    """

    def __init__(self, router: RouterLike, *, use_case: str = "embed") -> None:
        self.router = router
        self.use_case = use_case

    async def __call__(self, query: str, tenant_ctx: TenantContext) -> list[float]:
        def call() -> list[float]:
            result = self.router.embed(
                tenant_ctx, texts=[query], use_case=self.use_case
            )
            if not result.embeddings or not result.embeddings[0]:
                raise ProviderError(
                    "embedding provider returned no vector",
                    use_case=self.use_case,
                )
            return list(result.embeddings[0])

        return await asyncio.to_thread(call)


class GatewayJudge:
    """``JudgeFn``: the rails grader's model call."""

    def __init__(self, router: RouterLike, *, use_case: str = "judge") -> None:
        self.router = router
        self.use_case = use_case

    async def __call__(self, query: str, messages: list[dict], tenant_ctx: TenantContext) -> str:
        chunk = await asyncio.to_thread(
            lambda: self.router.chat(
                tenant_ctx, messages=messages, use_case=self.use_case
            )
        )
        return chunk.content or ""


GENERATION_SYSTEM_PROMPT = """\
Answer the question using only the numbered evidence passages.

Rules:
- Every factual sentence must end with a citation marker naming the passage it
  came from, for example [2] or [2][3].
- If the evidence does not answer the question, say so plainly. Do not use
  knowledge from outside the passages.
- Do not invent passage numbers. A marker outside the list is a rejected answer.
"""


def build_generation_messages(
    question: str,
    evidence: list[ScoredChunk],
    *,
    system_prompt: str = GENERATION_SYSTEM_PROMPT,
) -> list[dict]:
    """Render the grounded prompt.

    Truncation is per-passage and reported through the numbering rather than
    silent: dropping a passage silently would shift every marker after it.
    """
    blocks: list[str] = []
    for index, chunk in enumerate(evidence, start=1):
        location = ""
        if chunk.page_no is not None:
            location = f" (page {chunk.page_no}"
            if chunk.page_count:
                location += f" of {chunk.page_count}"
            location += ")"
        body = chunk.text or ""
        blocks.append(f"[{index}]{location}\n{body}")

    if blocks:
        user = "Evidence passages:\n\n" + "\n\n".join(blocks)
    else:
        user = "Evidence passages: (none)"
    user += f"\n\nQuestion: {question}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]
