"""Memory types: what a fact is, and how a fact came to be believed.

The extracted fact and the stored fact are different objects on purpose.
``ExtractedFact`` is a *proposal* produced by a model — it can be malformed, it
can contradict something already known, and it has no identity yet.
``MemoryFact`` is a stored, attributed, interval-bounded assertion.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.tenant import TenantContext

#: Signature of a chat completion. The tenant context is a parameter so the call
#: is metered and budget-checked: fact extraction is an LLM call per conversation
#: and belongs on the tenant's ledger like any other.
CompleteFn = Callable[[list[dict], TenantContext], Awaitable[str]]

#: Fact categories mirror the retrieval rule that long-term memory and enterprise
#: knowledge are never mixed: these are statements about the user and their work.
CATEGORIES = ("preference", "behavior", "context", "entity")


class FactCategory(StrEnum):
    PREFERENCE = "preference"
    BEHAVIOR = "behavior"
    CONTEXT = "context"
    ENTITY = "entity"


class FactOperation(StrEnum):
    #: A new fact in a slot nothing occupies.
    ADD = "add"
    #: The slot is already occupied; the prior fact is *closed*, not overwritten.
    REPLACE = "replace"


@dataclass(slots=True)
class ExtractedFact:
    """A model-proposed fact. ``subject_key`` names the slot it occupies."""

    fact: str
    category: FactCategory
    subject_key: str = ""
    confidence: float = 0.5
    operation: FactOperation = FactOperation.ADD
    #: When the fact became true. ``None`` means "now" — knowledge time is
    #: ingestion time unless the model asserted otherwise.
    valid_from: datetime | None = None

    def __post_init__(self) -> None:
        if not self.fact.strip():
            raise ValidationError("extracted fact text must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(
                "fact confidence must be within [0, 1]", confidence=self.confidence
            )
        if self.operation == FactOperation.REPLACE and not self.subject_key:
            # Replacing "the fact" is not a defined operation: without a slot
            # there is nothing specific to close, and guessing would invalidate
            # an unrelated fact.
            raise ValidationError(
                "a replace operation requires a subject_key naming the slot it replaces"
            )

    def valid_from_or_now(self, now: datetime) -> datetime:
        return self.valid_from or now


@dataclass(slots=True)
class MemoryFact:
    """A stored fact. Never mutated in place except to close its interval."""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    fact: str
    category: str
    confidence: float
    subject_key: str = ""
    decay_score: float = 1.0
    mention_count: int = 1
    valid_from: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed_at: datetime | None = None
    source_conversation_id: UUID | None = None
    superseded_by: UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.valid_until is None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "fact": self.fact,
            "category": self.category,
            "confidence": self.confidence,
            "subject_key": self.subject_key,
            "decay_score": round(self.decay_score, 4),
            "mention_count": self.mention_count,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "ingested_at": self.ingested_at.isoformat(),
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
            "source_conversation_id": (
                str(self.source_conversation_id) if self.source_conversation_id else None
            ),
            "superseded_by": str(self.superseded_by) if self.superseded_by else None,
        }
