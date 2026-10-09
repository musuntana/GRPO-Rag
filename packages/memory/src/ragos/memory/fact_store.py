"""Bi-temporal fact store.

Four rules this module exists to enforce (architecture §7.3, AGENTS.md memory
rules 8/10/13):

1. **A contradicting fact closes the prior one; it never overwrites it.** The
   predecessor keeps its text with ``valid_until`` set and ``superseded_by``
   pointing at its successor, so a point-in-time question has an answer.
2. **Recall returns active facts only**, unless the caller asks for a point in
   time. A superseded fact leaking into a prompt is the mechanism by which a
   corrected preference keeps producing the wrong answer.
3. **Decay is derived, not stored-and-trusted.** The stored column is a
   convenience; every read recomputes from the validity window and access
   recency, because a counter that drifts silently outlives the truth it tracks.
4. **The actor is required and scoped.** No ``user_id`` means no memory operation
   runs at all — a read with no user dimension is a disclosure, not a wider query.

``scope_key`` is a required keyword on every method and is checked against the
resolved context, so a caller cannot reach another actor's facts by passing a
different key.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ragos.core.errors import PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.memory.decay import DecayPolicy, decay_score, should_archive
from ragos.memory.types import ExtractedFact, FactOperation, MemoryFact
from ragos.storage.models.app import MemoryFact as MemoryFactRow
from sqlmodel import Session, select


class MemoryFactStore:
    """CRUD and recall for ``app.memory_fact``, bi-temporal and actor-scoped."""

    def __init__(
        self,
        session: Session,
        tenant_ctx: TenantContext,
        *,
        policy: DecayPolicy | None = None,
    ) -> None:
        tenant_ctx.require_private_scope_key()
        self.session = session
        self.tenant_ctx = tenant_ctx
        self.scope_key = tenant_ctx.require_private_scope_key()
        self.policy = policy or DecayPolicy()

    # -- scope --------------------------------------------------------------

    def _require_scope_key(self, scope_key: str) -> None:
        if not scope_key:
            raise PermissionDeniedError(
                "scope_key is required and must not be empty",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if scope_key != self.scope_key:
            raise PermissionDeniedError(
                "scope_key does not match the resolved tenant scope",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )

    def _require_user_id(self, user_id: UUID | None) -> UUID:
        if user_id is None:
            raise PermissionDeniedError(
                "memory operations require a user_id; there is nothing to scope the "
                "read to without one",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if user_id != self.tenant_ctx.require_user_id():
            raise PermissionDeniedError("memory user_id does not match resolved owner")
        return user_id

    def _actor_scoped(self, user_id: UUID):
        return (
            select(MemoryFactRow)
            .where(MemoryFactRow.tenant_id == self.tenant_ctx.tenant_id)
            .where(MemoryFactRow.user_id == user_id)
        )

    # -- writes -------------------------------------------------------------

    def record(
        self,
        *,
        scope_key: str,
        user_id: UUID | None,
        facts: Sequence[ExtractedFact],
        conversation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> list[MemoryFact]:
        """Store proposed facts, closing whatever they contradict.

        Idempotent under re-delivery of the same conversation: a fact already
        stored with the same text, slot, and knowledge time is counted as another
        mention rather than inserted again, because a retried writeback task
        would otherwise inflate ``mention_count`` — which is the first signal on
        the memory rerank ladder.
        """
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        moment = now or datetime.now(UTC)
        if not facts:
            return []

        closed_slots = {
            f.subject_key for f in facts if f.operation == FactOperation.REPLACE and f.subject_key
        }
        predecessors: list[MemoryFactRow] = []
        if closed_slots:
            statement = (
                self._actor_scoped(owner)
                .where(MemoryFactRow.valid_until.is_(None))  # noqa: E711
                .where(MemoryFactRow.subject_key.in_(sorted(closed_slots)))
            )
            predecessors = list(self.session.exec(statement).all())

        stored: list[MemoryFact] = []
        for candidate in facts:
            row = self.session.exec(
                select(MemoryFactRow)
                .where(MemoryFactRow.tenant_id == self.tenant_ctx.tenant_id)
                .where(MemoryFactRow.user_id == owner)
                .where(MemoryFactRow.fact == candidate.fact)
                .where(MemoryFactRow.subject_key == candidate.subject_key)
            ).first()
            if row is not None:
                # Re-delivery of a fact already known.
                row.mention_count += 1
                row.last_accessed_at = moment
                self.session.add(row)
                stored.append(self._to_domain(row, moment))
                continue

            new_row = MemoryFactRow(
                id=uuid4(),
                tenant_id=self.tenant_ctx.tenant_id,
                user_id=owner,
                conversation_id=conversation_id,
                category=str(candidate.category),
                fact=candidate.fact,
                confidence=candidate.confidence,
                decay_score=1.0,
                subject_key=candidate.subject_key,
                mention_count=1,
                valid_from=candidate.valid_from_or_now(moment),
                valid_until=None,
                ingested_at=moment,
                last_accessed_at=moment,
                superseded_by=None,
            )
            self.session.add(new_row)
            self.session.flush()
            stored.append(self._to_domain(new_row, moment))

        successors = new_row_ids(stored, candidate_slots=sorted(closed_slots))
        for predecessor in predecessors:
            # Close the interval, keep the text, record what replaced it.
            predecessor.valid_until = moment
            predecessor.superseded_by = successors.get(predecessor.subject_key)
            predecessor.decay_score = 0.0
            self.session.add(predecessor)

        return stored

    def touch(
        self,
        *,
        scope_key: str,
        user_id: UUID | None,
        fact_ids: Sequence[UUID],
        now: datetime | None = None,
    ) -> int:
        """Record an access: resets the recency clock, never the validity window.

        A fact outside its window is not revived by being read — it is closed
        because it stopped being true, and reading it changes nothing.
        """
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        if not fact_ids:
            return 0
        moment = now or datetime.now(UTC)
        statement = self._actor_scoped(owner).where(MemoryFactRow.id.in_(list(fact_ids)))
        touched = 0
        for row in self.session.exec(statement).all():
            if row.valid_until is not None and row.valid_until <= moment:
                continue
            row.last_accessed_at = moment
            row.decay_score = decay_score(
                valid_from=row.valid_from,
                valid_until=row.valid_until,
                ingested_at=row.ingested_at,
                last_accessed_at=moment,
                now=moment,
                half_life_days=self.policy.half_life_days,
            )
            self.session.add(row)
            touched += 1
        return touched

    def sweep(
        self,
        *,
        scope_key: str,
        user_id: UUID | None,
        now: datetime | None = None,
        retain_days: int = 90,
        limit: int = 500,
    ) -> list[UUID]:
        """Delete facts whose validity closed long enough ago.

        Closed facts are not deleted immediately: a point-in-time query may still
        name a window that includes them, and the retention period is what keeps
        that answerable. Returns the deleted ids so the caller can remove the
        matching vectors — a fact row that survives next to a deleted vector is a
        leak waiting to be rediscovered by a different recall path.
        """
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=retain_days)

        statement = (
            self._actor_scoped(owner)
            .where(MemoryFactRow.valid_until.is_not(None))  # noqa: E711
            .where(MemoryFactRow.valid_until <= cutoff)
            .limit(limit)
        )
        rows = list(self.session.exec(statement).all())
        deleted: list[UUID] = []
        for row in rows:
            deleted.append(row.id)
            self.session.delete(row)
        return deleted

    def delete_for_actor(self, *, scope_key: str, user_id: UUID | None) -> list[UUID]:
        """DSAR deletion: every fact attributed to one actor, superseded included.

        Superseded facts are deleted too. A withdrawn fact is still personal data,
        and "it is no longer true" is not an exemption from deletion.
        """
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        statement = self._actor_scoped(owner)
        rows = list(self.session.exec(statement).all())
        deleted: list[UUID] = []
        for row in rows:
            deleted.append(row.id)
            self.session.delete(row)
        return deleted

    # -- reads --------------------------------------------------------------

    def recall(
        self,
        *,
        scope_key: str,
        user_id: UUID | None,
        subject_keys: Sequence[str] | None = None,
        limit: int = 10,
        now: datetime | None = None,
        include_archived: bool = False,
        as_of: datetime | None = None,
    ) -> list[MemoryFact]:
        """Active facts, most usable first. ``as_of`` switches to point-in-time."""
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        moment = now or datetime.now(UTC)

        statement = self._actor_scoped(owner)
        if as_of is not None:
            statement = statement.where(MemoryFactRow.valid_from <= as_of).where(
                (MemoryFactRow.valid_until.is_(None))  # noqa: E711
                | (MemoryFactRow.valid_until > as_of)
            )
        else:
            statement = statement.where(MemoryFactRow.valid_until.is_(None))  # noqa: E711
        if subject_keys:
            statement = statement.where(MemoryFactRow.subject_key.in_(list(subject_keys)))

        scored: list[MemoryFact] = []
        for row in self.session.exec(statement).all():
            fact = self._to_domain(row, moment)
            if not include_archived and should_archive(fact.decay_score, self.policy):
                continue
            scored.append(fact)

        scored.sort(key=lambda f: (f.decay_score, f.mention_count), reverse=True)
        return scored[:limit]

    def as_of(
        self,
        *,
        scope_key: str,
        user_id: UUID | None,
        at: datetime,
        subject_keys: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryFact]:
        """What the system believed at a point in time.

        "What did we agree on last month" is a different question from "what is
        true now", and it is only answerable because superseded rows keep their
        text and their interval.
        """
        return self.recall(
            scope_key=scope_key,
            user_id=user_id,
            subject_keys=subject_keys,
            limit=limit,
            as_of=at,
            include_archived=True,
        )

    def export_for_actor(self, *, scope_key: str, user_id: UUID | None) -> list[MemoryFact]:
        """Every fact attributed to one actor, including superseded ones."""
        self._require_scope_key(scope_key)
        owner = self._require_user_id(user_id)
        rows = list(self.session.exec(self._actor_scoped(owner)).all())
        return [self._to_domain(row, datetime.now(UTC)) for row in rows]

    # -- mapping ------------------------------------------------------------

    def _to_domain(self, row: MemoryFactRow, now: datetime) -> MemoryFact:
        return MemoryFact(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,  # type: ignore[arg-type]
            fact=row.fact,
            category=row.category,
            confidence=row.confidence,
            subject_key=row.subject_key,
            decay_score=decay_score(
                valid_from=row.valid_from,
                valid_until=row.valid_until,
                ingested_at=row.ingested_at,
                last_accessed_at=row.last_accessed_at,
                now=now,
                half_life_days=self.policy.half_life_days,
            ),
            mention_count=row.mention_count,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            ingested_at=row.ingested_at,
            last_accessed_at=row.last_accessed_at,
            source_conversation_id=row.conversation_id,
            superseded_by=row.superseded_by,
        )


def new_row_ids(stored: Sequence[MemoryFact], *, candidate_slots: Sequence[str]) -> dict[str, UUID]:
    """Newest fact id per closed slot, so a predecessor can point at its successor."""
    slots = set(candidate_slots)
    out: dict[str, UUID] = {}
    for fact in stored:
        if fact.subject_key in slots:
            out[fact.subject_key] = fact.id
    return out
