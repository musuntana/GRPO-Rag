"""Query-planning types shared by the query engine and the pipeline.

A plan is the answer to "what text do we actually retrieve with". Keeping it a
value object rather than a mutated string means the retrieval explain can record
every query that ran, which is the difference between diagnosing a bad
retrieval and guessing at one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One prior turn. ``text`` only — roles matter for the rewriter, content
    formatting matters for the prompt builder, and neither belongs in the query
    engine."""

    role: Role
    text: str


@dataclass(slots=True)
class QueryPlan:
    """The retrieval-facing form of a user question.

    ``queries`` is what the recall channels run, one call per entry. A
    decomposed question therefore costs N recall round-trips, which is why the
    strategy is a profile decision rather than something the engine picks.
    """

    strategy: str
    #: The single query used for grading, refinement, and generation.
    primary: str
    queries: tuple[str, ...]
    hypothetical: str | None = None
    sub_queries: tuple[str, ...] = ()
    used_history: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "primary": self.primary,
            "queries": list(self.queries),
            "hypothetical": self.hypothetical,
            "sub_queries": list(self.sub_queries),
            "used_history": self.used_history,
            "notes": list(self.notes),
        }
