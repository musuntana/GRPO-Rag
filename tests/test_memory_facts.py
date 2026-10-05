"""Long-term memory: bi-temporal facts, derived decay, actor scope.

Architecture §7.3. The claims under test:

- a contradicting fact **closes** the prior one instead of overwriting it
- a superseded fact stays queryable, and a point-in-time query answers from the
  interval rather than from whatever is current
- decay comes from access recency *inside* the validity window, so an access
  cannot revive a fact that stopped being true
- an operation without a ``user_id`` refuses rather than widening to the tenant
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.memory.decay import (
    DEFAULT_HALF_LIFE_DAYS,
    DecayPolicy,
    decay_score,
    is_expired,
    should_archive,
)
from ragos.memory.extractor import parse_extraction
from ragos.memory.fact_store import MemoryFactStore
from ragos.memory.types import (
    ExtractedFact,
    FactCategory,
    FactOperation,
)
from ragos.storage.models.app import MemoryFact as MemoryFactRow

TENANT_ID = uuid4()
OTHER_TENANT_ID = uuid4()
USER_ID = uuid4()
OTHER_USER_ID = uuid4()
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _ctx(*, user_id=None) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        actor_id="u_1",
        trace_id="trace-mem",
        user_id=user_id,
    )


class _Session:
    """A staged stand-in for the fact table.

    Predicates are read off the compiled statement — bound parameter values from
    ``compile().params``, operators from the SQL text — so these tests exercise the
    real filters (tenant, actor, interval) rather than a hand-written imitation of
    them. What it does not verify is that the SQL itself is valid; that is what the
    migration and a live database are for.

    Two limits are deliberate: ordering and LIMIT are ignored (tests assert on the
    set and on the store's own sort), and an unrecognised predicate matches
    everything rather than raising, so a new filter fails loudly in the assertions
    that care about it instead of erroring here.
    """

    def __init__(self, rows: list[MemoryFactRow] | None = None) -> None:
        self.rows: list[MemoryFactRow] = list(rows or [])
        self.added: list[MemoryFactRow] = []
        self.deleted: list[MemoryFactRow] = []
        self.statements: list[str] = []

    def exec(self, statement):
        sql = str(statement)
        params = statement.compile().params
        self.statements.append(sql)
        matched = self._match(sql, params)

        class _Result:
            @staticmethod
            def all():
                return list(matched)

            @staticmethod
            def first():
                return matched[0] if matched else None

        return _Result()

    def _match(self, sql: str, params: dict) -> list[MemoryFactRow]:
        rows = list(self.rows)

        for name, value in params.items():
            field = name.rsplit("_", 1)[0] if name[-1:].isdigit() else name
            operator = _operator(sql, field)
            if field in ("tenant_id", "user_id", "fact"):
                rows = [r for r in rows if getattr(r, field) == value]
            elif field == "subject_key":
                rows = [r for r in rows if r.subject_key in _expand(value)]
            elif field == "id":
                rows = [r for r in rows if r.id in _expand(value)]
            elif field == "valid_from":
                rows = [r for r in rows if _compare(r.valid_from, operator, value)]
            elif field == "valid_until":
                # A NULL interval has no ordering: only an explicit IS [NOT] NULL
                # predicate, handled below, decides whether it is in scope.
                rows = [r for r in rows if r.valid_until is None or _compare(
                    r.valid_until, operator, value
                )]

        # IS [NOT] NULL counts only when it is the *whole* predicate for the column.
        # In a point-in-time query ``valid_until IS NULL OR valid_until > :at`` is
        # an accepted branch, not a filter, and applying it as one empties the result.
        if _operator(sql, "valid_until") == "=":
            if "valid_until IS NULL" in sql:
                rows = [r for r in rows if r.valid_until is None]
            if "valid_until IS NOT NULL" in sql:
                rows = [r for r in rows if r.valid_until is not None]
        return rows

    def add(self, row: MemoryFactRow) -> None:
        if row not in self.rows:
            self.rows.append(row)
        self.added.append(row)

    def flush(self) -> None:
        return None

    def refresh(self, _row) -> None:
        return None

    def delete(self, row: MemoryFactRow) -> None:
        self.deleted.append(row)
        if row in self.rows:
            self.rows.remove(row)


def _operator(sql: str, field: str) -> str:
    """Read the comparison operator out of the WHERE clause for one column.

    Scans every mention rather than the first: a column can appear twice in one
    predicate (``valid_until IS NULL OR valid_until > :x``) and only the comparison
    carries the operator.
    """
    pattern = rf"app\.memory_fact\.{field}\s*(<=|>=|<|>|=)\s*:"
    found = re.search(pattern, sql)
    return found.group(1) if found else "="


def _compare(value, operator: str, bound) -> bool:
    if operator == "<=":
        return value <= bound
    if operator == "<":
        return value < bound
    if operator == ">=":
        return value >= bound
    if operator == ">":
        return value > bound
    return value == bound


def _expand(value):
    """SQLAlchemy renders an ``IN`` list as one param per element."""
    if isinstance(value, (list, tuple, set)):
        return set(value)
    return {value}


def _store(rows=None, *, policy: DecayPolicy | None = None) -> tuple[MemoryFactStore, _Session]:
    session = _Session(rows)
    return (
        MemoryFactStore(session=session, tenant_ctx=_ctx(user_id=USER_ID), policy=policy),
        session,
    )


def _row(
    fact: str,
    *,
    subject_key: str = "",
    valid_from: datetime = NOW - timedelta(days=10),
    valid_until: datetime | None = None,
    mention_count: int = 1,
    user_id=USER_ID,
    tenant_id=TENANT_ID,
    ingested_at: datetime | None = None,
    last_accessed_at: datetime | None = None,
) -> MemoryFactRow:
    return MemoryFactRow(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=uuid4(),
        category="preference",
        fact=fact,
        confidence=0.9,
        decay_score=1.0,
        subject_key=subject_key,
        mention_count=mention_count,
        valid_from=valid_from,
        valid_until=valid_until,
        ingested_at=ingested_at or valid_from,
        last_accessed_at=last_accessed_at,
    )


def _proposal(fact: str, **kwargs) -> ExtractedFact:
    fields = {
        "fact": fact,
        "category": FactCategory.PREFERENCE,
        "subject_key": "language_preference",
        "confidence": 0.9,
        "operation": FactOperation.REPLACE,
    }
    fields.update(kwargs)
    return ExtractedFact(**fields)


# -- decay ------------------------------------------------------------------


def test_a_fact_outside_its_window_scores_zero_however_often_it_was_read() -> None:
    """Access cannot resurrect a statement that stopped being true."""
    score = decay_score(
        valid_from=NOW - timedelta(days=400),
        valid_until=NOW - timedelta(days=1),
        ingested_at=NOW - timedelta(days=400),
        last_accessed_at=NOW,
        now=NOW,
    )

    assert score == 0.0
    assert is_expired(valid_until=NOW - timedelta(days=1), now=NOW)


def test_decay_is_recency_within_the_window() -> None:
    fresh = decay_score(
        valid_from=NOW,
        valid_until=None,
        ingested_at=NOW,
        last_accessed_at=None,
        now=NOW,
    )
    stale = decay_score(
        valid_from=NOW - timedelta(days=DEFAULT_HALF_LIFE_DAYS),
        valid_until=None,
        ingested_at=NOW - timedelta(days=DEFAULT_HALF_LIFE_DAYS),
        last_accessed_at=None,
        now=NOW,
    )

    assert fresh == pytest.approx(1.0)
    # One half-life of not being touched lands at a half.
    assert stale == pytest.approx(0.5, abs=0.01)


def test_an_access_resets_the_recency_clock() -> None:
    ingested = NOW - timedelta(days=DEFAULT_HALF_LIFE_DAYS * 3)
    unread = decay_score(
        valid_from=ingested,
        valid_until=None,
        ingested_at=ingested,
        last_accessed_at=None,
        now=NOW,
    )
    just_read = decay_score(
        valid_from=ingested,
        valid_until=None,
        ingested_at=ingested,
        last_accessed_at=NOW,
        now=NOW,
    )

    assert just_read > unread
    assert just_read == pytest.approx(1.0)


def test_a_fact_is_ingested_at_the_time_it_was_learned_not_when_it_became_true() -> None:
    """A fact stated last month and learned today has not decayed."""
    score = decay_score(
        valid_from=NOW - timedelta(days=90),
        valid_until=None,
        ingested_at=NOW,
        last_accessed_at=None,
        now=NOW,
    )

    assert score == pytest.approx(1.0)


def test_archive_threshold_is_a_recorded_value() -> None:
    assert should_archive(0.01, DecayPolicy(archive_threshold=0.05)) is True
    assert should_archive(0.2, DecayPolicy(archive_threshold=0.05)) is False


# -- actor and tenant scope -------------------------------------------------


def test_a_read_without_a_user_id_refuses() -> None:
    """One end user's memory is not the tenant's memory."""
    store = MemoryFactStore(session=_Session(), tenant_ctx=_ctx())

    with pytest.raises(PermissionDeniedError, match="user_id"):
        store.recall(scope_key=store.scope_key, user_id=None)


def test_a_foreign_scope_key_is_refused() -> None:
    store, _ = _store()

    with pytest.raises(PermissionDeniedError, match="does not match"):
        store.recall(scope_key=f"{OTHER_TENANT_ID}::u_1", user_id=USER_ID)


def test_the_store_refuses_to_exist_without_a_scope_key() -> None:
    ctx = _ctx(user_id=USER_ID)
    ctx.scope_key = ""

    with pytest.raises(PermissionDeniedError, match="scope_key"):
        MemoryFactStore(session=_Session(), tenant_ctx=ctx)


def test_recall_is_scoped_to_the_actor() -> None:
    store, _ = _store(
        [
            _row("mine", user_id=USER_ID),
            _row("someone else's", user_id=OTHER_USER_ID),
        ]
    )

    recalled = store.recall(scope_key=store.scope_key, user_id=USER_ID)

    assert [f.fact for f in recalled] == ["mine"]


# -- bi-temporal recording --------------------------------------------------


def test_a_contradicting_fact_closes_the_prior_one_without_erasing_it() -> None:
    prior = _row("I live in Berlin", subject_key="city_of_residence")
    store, _ = _store([prior])

    stored = store.record(
        scope_key=store.scope_key,
        user_id=USER_ID,
        facts=[_proposal("I live in Lisbon", subject_key="city_of_residence")],
        now=NOW,
    )

    assert prior.fact == "I live in Berlin", "the superseded text must survive"
    assert prior.valid_until == NOW
    assert prior.superseded_by == stored[0].id
    assert prior.decay_score == 0.0
    assert stored[0].valid_until is None


def test_recall_returns_the_current_fact_and_not_the_superseded_one() -> None:
    store, _ = _store(
        [
            _row("I live in Berlin", subject_key="city_of_residence",
                 valid_until=NOW - timedelta(days=1)),
            _row("I live in Lisbon", subject_key="city_of_residence",
                 valid_from=NOW - timedelta(days=1)),
        ]
    )

    recalled = store.recall(scope_key=store.scope_key, user_id=USER_ID)

    assert [f.fact for f in recalled] == ["I live in Lisbon"]


def test_a_point_in_time_query_answers_from_the_interval() -> None:
    store, _ = _store(
        [
            _row("I live in Berlin", subject_key="city_of_residence",
                 valid_from=NOW - timedelta(days=200),
                 valid_until=NOW - timedelta(days=100)),
            _row("I live in Lisbon", subject_key="city_of_residence",
                 valid_from=NOW - timedelta(days=100)),
        ]
    )

    earlier = store.as_of(
        scope_key=store.scope_key, user_id=USER_ID, at=NOW - timedelta(days=150)
    )

    assert [f.fact for f in earlier] == ["I live in Berlin"]


def test_a_point_in_time_query_before_any_fact_is_empty() -> None:
    store, _ = _store([_row("I live in Lisbon", valid_from=NOW - timedelta(days=10))])

    earlier = store.as_of(
        scope_key=store.scope_key, user_id=USER_ID, at=NOW - timedelta(days=30)
    )

    assert earlier == []


def test_a_new_fact_in_an_unrelated_slot_leaves_the_old_one_alone() -> None:
    existing = _row("I prefer metric units", subject_key="units_preference")
    store, _ = _store([existing])

    store.record(
        scope_key=store.scope_key,
        user_id=USER_ID,
        facts=[_proposal("I live in Lisbon", subject_key="city_of_residence")],
        now=NOW,
    )

    assert existing.valid_until is None
    assert existing.superseded_by is None


def test_redelivering_a_fact_counts_a_mention_instead_of_inserting_it() -> None:
    """The writeback is a retried task; a retry must not inflate the top ladder signal."""
    store, session = _store([_row("I prefer metric units", subject_key="units_preference")])

    stored = store.record(
        scope_key=store.scope_key,
        user_id=USER_ID,
        facts=[
            _proposal(
                "I prefer metric units", subject_key="units_preference", operation=FactOperation.ADD
            )
        ],
        now=NOW,
    )

    assert len(session.rows) == 1
    assert session.rows[0].mention_count == 2
    assert stored[0].mention_count == 2


def test_recall_can_be_narrowed_to_slots() -> None:
    store, _ = _store(
        [
            _row("units", subject_key="units_preference"),
            _row("city", subject_key="city_of_residence"),
        ]
    )

    recalled = store.recall(
        scope_key=store.scope_key, user_id=USER_ID, subject_keys=["units_preference"]
    )

    assert [f.fact for f in recalled] == ["units"]


def test_recall_ranks_by_derived_decay_then_mention_frequency() -> None:
    store, _ = _store(
        [
            _row("recent", mention_count=1, last_accessed_at=NOW),
            _row("stale", mention_count=9, ingested_at=NOW - timedelta(days=120)),
        ]
    )

    recalled = store.recall(scope_key=store.scope_key, user_id=USER_ID)

    assert [f.fact for f in recalled][0] == "recent"


def test_an_archived_fact_is_excluded_from_recall_unless_asked_for() -> None:
    ancient = NOW - timedelta(days=DEFAULT_HALF_LIFE_DAYS * 10)
    store, _ = _store(
        [_row("ancient", valid_from=ancient, ingested_at=ancient, last_accessed_at=ancient)]
    )

    assert store.recall(scope_key=store.scope_key, user_id=USER_ID) == []
    assert len(store.recall(scope_key=store.scope_key, user_id=USER_ID, include_archived=True)) == 1


# -- access, sweep, deletion ------------------------------------------------


def test_touching_a_closed_fact_changes_nothing() -> None:
    closed = _row("old", valid_until=NOW - timedelta(days=1))
    store, _ = _store([closed])

    touched = store.touch(scope_key=store.scope_key, user_id=USER_ID, fact_ids=[closed.id], now=NOW)

    assert touched == 0
    assert closed.last_accessed_at is None


def test_touching_an_active_fact_resets_its_clock() -> None:
    active = _row("live", ingested_at=NOW - timedelta(days=100), last_accessed_at=None)
    store, _ = _store([active])

    touched = store.touch(scope_key=store.scope_key, user_id=USER_ID, fact_ids=[active.id], now=NOW)

    assert touched == 1
    assert active.last_accessed_at == NOW
    assert active.decay_score == pytest.approx(1.0)


def test_the_sweep_keeps_recently_closed_facts_for_point_in_time_queries() -> None:
    old = _row("long closed", valid_until=NOW - timedelta(days=365))
    recent = _row("recently closed", valid_until=NOW - timedelta(days=2))
    store, _ = _store([old, recent])

    deleted = store.sweep(scope_key=store.scope_key, user_id=USER_ID, now=NOW, retain_days=90)

    assert deleted == [old.id]
    assert len(store.export_for_actor(scope_key=store.scope_key, user_id=USER_ID)) == 1


def test_deletion_for_an_actor_removes_superseded_facts_too() -> None:
    """Withdrawn facts are still personal data."""
    store, _ = _store(
        [
            _row("superseded", valid_until=NOW - timedelta(days=1)),
            _row("current"),
            _row("other user", user_id=OTHER_USER_ID),
        ]
    )

    deleted = store.delete_for_actor(scope_key=store.scope_key, user_id=USER_ID)

    assert len(deleted) == 2
    remaining = store.export_for_actor(scope_key=store.scope_key, user_id=USER_ID)
    assert remaining == []


def test_export_includes_every_version_of_every_fact() -> None:
    store, _ = _store(
        [
            _row("v1", valid_until=NOW - timedelta(days=1)),
            _row("v2"),
        ]
    )

    exported = store.export_for_actor(scope_key=store.scope_key, user_id=USER_ID)

    assert {f.fact for f in exported} == {"v1", "v2"}
    assert sum(1 for f in exported if not f.is_active) == 1


# -- extraction -------------------------------------------------------------


def test_extraction_parses_slots_categories_and_operations() -> None:
    facts = parse_extraction(
        '{"facts": ['
        '{"subject_key": "units_preference", "fact": "uses metric", "category": "preference",'
        ' "confidence": 0.9, "operation": "add"},'
        '{"subject_key": "city_of_residence", "fact": "moved to Lisbon", "category": "context",'
        ' "confidence": 0.8, "operation": "replace"}]}'
    )

    assert [f.subject_key for f in facts] == ["units_preference", "city_of_residence"]
    assert facts[1].operation == FactOperation.REPLACE


def test_a_malformed_extraction_stores_nothing() -> None:
    assert parse_extraction("I think the user prefers metric units") == []
    assert parse_extraction('{"facts": "not a list"}') == []


def test_a_record_with_no_text_is_dropped_rather_than_guessed() -> None:
    assert parse_extraction('{"facts": [{"fact": "", "category": "preference"}]}') == []


def test_an_unknown_category_is_kept_at_reduced_confidence() -> None:
    """Losing a stated preference costs recall; mis-filing one only costs routing."""
    facts = parse_extraction(
        '{"facts": [{"subject_key": "k", "fact": "likes tea", "category": "vibes",'
        ' "confidence": 0.95}]}'
    )

    assert facts[0].category == FactCategory.CONTEXT
    assert facts[0].confidence == pytest.approx(0.3)


def test_a_replace_without_a_slot_is_dropped() -> None:
    """Without a slot there is nothing specific to close, and guessing would
    invalidate an unrelated fact."""
    facts = parse_extraction(
        '{"facts": [{"subject_key": "", "fact": "changed my mind",'
        ' "category": "preference", "operation": "replace"}]}'
    )

    assert facts == []


def test_a_replace_proposal_without_a_slot_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError, match="subject_key"):
        ExtractedFact(
            fact="changed my mind",
            category=FactCategory.PREFERENCE,
            operation=FactOperation.REPLACE,
        )


def test_an_empty_fact_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        ExtractedFact(fact="   ", category=FactCategory.CONTEXT)


def test_confidence_outside_the_unit_interval_is_rejected() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ExtractedFact(fact="x", category=FactCategory.CONTEXT, confidence=1.4)
