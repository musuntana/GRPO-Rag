"""Migration and model metadata must not drift apart.

The drift these tests exist to prevent is specific: a migration adds a column
that no model declares, or a model declares a column the migration never
creates. Both fail only at runtime against a live database, and the second one
fails silently as an insert error on a tenant's ingest path.

No live database is required — the migration module is imported and its
`upgrade()` body is inspected as a call graph, with the column set derived from
the SQLModel metadata it is supposed to match.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from ragos.storage.models.app import (
    EvalCase,
    EvalRun,
    FeedbackEntry,
    GraderValidation,
    MemoryFact,
    MemoryProfileVersion,
    MemorySummary,
    MessageIndex,
    ModelProfile,
    RerankerArtifact,
    RetrieverProfileVersion,
)
from ragos.storage.models.search import (
    SearchChunkHot,
    SearchChunkStable,
    SearchMemory,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infra" / "migrations" / "versions"


def _load(revision: str):
    path = MIGRATIONS_DIR / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpRecorder:
    """Stands in for ``alembic.op`` and records every statement.

    Recording rather than parsing: the migration builds its statements through
    helper functions and loops, so an AST reader has to re-implement Python to
    resolve them. Running ``upgrade()`` against a recorder resolves all of it
    and needs no database — which is the point, since the drift being guarded
    against otherwise only shows up against a live instance.
    """

    def __init__(self) -> None:
        self.created: dict[str, set[str]] = {}
        self.added: dict[str, set[str]] = {}
        self.executed: list[str] = []
        self.dropped_tables: list[str] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def create_table(self, name: str, *columns, **kwargs) -> None:
        self.created[name] = {c.name for c in columns if hasattr(c, "name")}

    def add_column(self, table: str, column, **_kw) -> None:
        self.added.setdefault(table, set()).add(column.name)

    def create_index(self, *_a, **_kw) -> None:
        pass

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def drop_table(self, name: str, **_kw) -> None:
        self.dropped_tables.append(name)

    def drop_column(self, table_name: str, column_name: str, **_kw) -> None:
        # Signature mirrors ``alembic.operations.Operations.drop_column``
        # deliberately: a migration that calls it with the arguments swapped
        # raises TypeError here instead of halfway through a production
        # rollback.
        self.dropped_columns.append((table_name, column_name))

    def drop_index(self, *_a, **_kw) -> None:
        pass


def _run_migration(revision: str, *, direction: str = "upgrade") -> OpRecorder:
    module = _load(revision)
    recorder = OpRecorder()
    original = module.op
    module.op = recorder  # type: ignore[assignment]
    try:
        getattr(module, direction)()
    finally:
        module.op = original  # type: ignore[assignment]
    return recorder


@pytest.fixture(scope="module")
def upgrade() -> OpRecorder:
    return _run_migration("0005_page_anchored_eval")


@pytest.fixture(scope="module")
def profile_gate_upgrade() -> OpRecorder:
    return _run_migration("0006_retriever_profile_gate")


@pytest.fixture(scope="module")
def memory_upgrade() -> OpRecorder:
    return _run_migration("0007_memory_bitemporal")


@pytest.fixture(scope="module")
def chat_upgrade() -> OpRecorder:
    return _run_migration("0008_chat_feedback")


# -- migration chain --------------------------------------------------------


def test_migration_chain_is_linear_and_unbroken() -> None:
    revisions: dict[str, str | None] = {}
    for path in sorted(MIGRATIONS_DIR.glob("0*.py")):
        module = _load(path.stem)
        revisions[module.revision] = module.down_revision

    roots = [r for r, down in revisions.items() if down is None]
    assert len(roots) == 1, "exactly one migration root"

    seen: set[str] = set()
    current = roots[0]
    while current is not None:
        assert current not in seen, "cycle in the migration chain"
        seen.add(current)
        children = [r for r, down in revisions.items() if down == current]
        assert len(children) <= 1, f"{current} has more than one child"
        current = children[0] if children else None

    assert seen == set(revisions), "some migration is unreachable from the root"


def test_the_new_migration_follows_the_model_gateway_migration() -> None:
    module = _load("0005_page_anchored_eval")

    assert module.down_revision == "0004_model_gateway"


def test_the_profile_gate_migration_follows_the_page_anchored_migration() -> None:
    module = _load("0006_retriever_profile_gate")

    assert module.down_revision == "0005_page_anchored_eval"


def test_the_memory_migration_follows_the_profile_gate_migration() -> None:
    module = _load("0007_memory_bitemporal")

    assert module.down_revision == "0006_retriever_profile_gate"


def test_the_chat_migration_follows_the_memory_migration() -> None:
    module = _load("0008_chat_feedback")

    assert module.down_revision == "0007_memory_bitemporal"


# -- app tables -------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        RetrieverProfileVersion,
        EvalCase,
        EvalRun,
        GraderValidation,
        RerankerArtifact,
    ],
)
def test_every_new_app_table_is_created_by_the_migration(model, upgrade) -> None:
    assert model.__tablename__ in upgrade.created


@pytest.mark.parametrize(
    "model,expected_columns",
    [
        (
            EvalCase,
            {
                "question",
                "gold_evidence",
                "expected_answer",
                "answerable",
                "supported_claims",
                "review_state",
                "labeler",
                "split",
            },
        ),
        (
            EvalRun,
            {"bundle_id", "bundle", "metrics", "case_total", "split", "status"},
        ),
        (
            GraderValidation,
            {"rails_prompt_version", "sample_size", "agreement", "confusion"},
        ),
        (
            RerankerArtifact,
            {"version", "method", "scope_key", "artifact_ref", "is_promoted"},
        ),
        (
            RetrieverProfileVersion,
            {
                "profile_id",
                "version",
                "join_mode",
                "channel_budgets",
                "crag_thresholds",
                "rerank_model_profile",
                "citation_verify_semantic",
                "top_k_recall",
                "top_k_rerank",
                "top_k_generate",
            },
        ),
    ],
)
def test_every_new_table_column_is_created(
    model, expected_columns: set[str], upgrade
) -> None:
    assert expected_columns <= upgrade.created[model.__tablename__]


def test_the_eval_case_grade_is_a_jsonb_grade_not_a_boolean() -> None:
    """Graded gold evidence is what makes mining useful; a boolean cannot express it."""
    from sqlalchemy.dialects.postgresql import JSONB

    column = EvalCase.model_fields["gold_evidence"].sa_column

    assert isinstance(column.type, JSONB)


def test_eval_case_grades_are_stored_whole_not_flattened() -> None:
    model = EvalCase.model_fields["gold_evidence"]

    assert model.default_factory() == []


def test_memory_carries_a_user_dimension() -> None:
    """Per-user export and deletion are not expressible without it."""
    assert "user_id" in SearchMemory.model_fields


# -- search schema ----------------------------------------------------------


@pytest.mark.parametrize(
    "model", [SearchChunkStable, SearchChunkHot, SearchMemory]
)
def test_every_search_table_gets_the_page_and_citation_columns(model, upgrade) -> None:
    added = upgrade.added.get(model.__tablename__, set())

    assert {
        "page_no",
        "page_count",
        "page_offset_start",
        "page_offset_end",
        "element_type",
        "start_char_idx",
        "end_char_idx",
        "is_parent",
        "brand_id",
        "source_type",
        "security_level",
        "page_image_ref",
    } <= added


@pytest.mark.parametrize(
    "model", [SearchChunkStable, SearchChunkHot, SearchMemory]
)
def test_the_model_declares_every_column_the_migration_adds(model, upgrade) -> None:
    """The reverse direction: a model column no migration creates fails at insert time."""
    added = upgrade.added.get(model.__tablename__, set())

    declared = set(model.model_fields)
    # timestamp columns come from the shared base class.
    declared -= {"id", "created_at", "updated_at"}

    assert added <= declared, f"migration adds columns absent from {model.__tablename__}"


def test_search_memory_gets_its_user_id_column(upgrade) -> None:
    assert "user_id" in upgrade.added["memory"]


def test_chunk_manifest_records_the_hierarchy_strategy(upgrade) -> None:
    assert {
        "parent_unit",
        "page_count",
        "parser_version",
        "chunker_version",
    } <= upgrade.added["chunk_manifest"]


def test_model_profile_gains_an_artifact_reference(upgrade) -> None:
    assert "artifact_ref" in upgrade.added["model_profile"]
    assert "artifact_ref" in ModelProfile.model_fields


def test_every_new_tenant_table_gets_row_level_security(upgrade) -> None:
    """Defense in depth, not the enforcement point — but it still has to exist."""
    source = "\n".join(upgrade.executed)

    for table in (
        "retriever_profile_version",
        "eval_case",
        "eval_run",
        "grader_validation",
        "reranker_artifact",
    ):
        assert f'"{table}" ENABLE ROW LEVEL SECURITY' in source
        assert f"CREATE POLICY tenant_isolation_{table}" in source


def test_the_migration_can_be_downgraded() -> None:
    """An irreversible migration is an operational dead end."""
    downgrade = _run_migration("0005_page_anchored_eval", direction="downgrade")

    assert "eval_case" in downgrade.dropped_tables
    assert "retriever_profile_version" in downgrade.dropped_tables
    assert "reranker_artifact" in downgrade.dropped_tables


# -- 0006: retriever profile gate flag --------------------------------------


def test_the_gate_flag_is_stored_so_it_can_be_switched_off(profile_gate_upgrade) -> None:
    """A gate that cannot be turned off per profile cannot be A/B'd, and thresholds
    that cannot be compared against a no-gate run are unfalsifiable."""
    assert {"crag_enabled", "name"} <= profile_gate_upgrade.added["retriever_profile_version"]
    assert {"crag_enabled", "name"} <= set(RetrieverProfileVersion.model_fields)


def test_existing_versions_keep_their_gate(profile_gate_upgrade) -> None:
    """The default is ``true``: a row written before the column existed had a gate,
    and there is no evidence it did not want one."""
    assert any(
        "crag_enabled" in sql and "true" in sql.lower() for sql in profile_gate_upgrade.executed
    )


def test_the_model_declares_every_column_the_gate_migration_adds(profile_gate_upgrade) -> None:
    added = profile_gate_upgrade.added["retriever_profile_version"]

    assert added <= set(RetrieverProfileVersion.model_fields)


def test_the_search_migration_drops_columns_with_alembics_argument_order() -> None:
    """Guards the 0005 downgrade, which passed (column, table_name=...) and would
    have raised TypeError mid-rollback."""
    downgrade = _run_migration("0005_page_anchored_eval", direction="downgrade")

    dropped = set(downgrade.dropped_columns)
    assert ("memory", "page_no") in dropped
    assert ("chunk_stable", "page_no") in dropped
    assert ("chunk_hot", "page_no") in dropped


def test_the_gate_migration_can_be_downgraded() -> None:
    """An irreversible migration is an operational dead end. This one drops the
    columns it added rather than the table 0005 created."""
    downgrade = _run_migration("0006_retriever_profile_gate", direction="downgrade")

    assert ("retriever_profile_version", "crag_enabled") in downgrade.dropped_columns
    assert ("retriever_profile_version", "name") in downgrade.dropped_columns


# -- 0007: bi-temporal memory ------------------------------------------------


def test_a_fact_carries_its_validity_interval_and_knowledge_time(memory_upgrade) -> None:
    """Point-in-time memory is only answerable if the interval is stored."""
    added = memory_upgrade.added["memory_fact"]

    assert {"valid_from", "valid_until", "ingested_at", "superseded_by"} <= added
    assert {"valid_from", "valid_until", "ingested_at", "superseded_by"} <= set(
        MemoryFact.model_fields
    )


def test_a_fact_is_attributed_to_an_actor(memory_upgrade) -> None:
    """Per-user export and DSAR deletion are not expressible without it."""
    assert "user_id" in memory_upgrade.added["memory_fact"]
    assert "user_id" in MemoryFact.model_fields


def test_the_fact_carries_a_slot_and_a_mention_count(memory_upgrade) -> None:
    """A contradiction needs a named slot; the rerank ladder needs the count."""
    added = memory_upgrade.added["memory_fact"]
    assert {"subject_key", "mention_count"} <= added
    assert {"subject_key", "mention_count"} <= set(MemoryFact.model_fields)


def test_the_summary_records_the_window_it_covers(memory_upgrade) -> None:
    added = memory_upgrade.added["memory_summary"]
    assert {"turn_range_start", "turn_range_end", "summary_version"} <= added
    assert {"turn_range_start", "turn_range_end", "summary_version"} <= set(
        MemorySummary.model_fields
    )


def test_the_model_declares_every_column_the_memory_migration_adds(memory_upgrade) -> None:
    for table, model in (("memory_fact", MemoryFact), ("memory_summary", MemorySummary)):
        added = memory_upgrade.added[table]
        assert added <= set(model.model_fields), f"{table} migration adds an undeclared column"


def test_the_summary_window_parameter_is_stored_not_defaulted() -> None:
    """``N`` is an evaluated parameter: no server default, and no model default.

    A default here would be the ``N`` that reached production without anyone
    choosing it, which is the exact failure the rule exists to prevent.
    """
    assert "window_n" in MemoryProfileVersion.model_fields
    assert MemoryProfileVersion.model_fields["window_n"].is_required()


def test_the_memory_profile_table_gets_row_level_security(memory_upgrade) -> None:
    source = "\n".join(memory_upgrade.executed)
    assert '"memory_profile_version" ENABLE ROW LEVEL SECURITY' in source
    assert "CREATE POLICY tenant_isolation_memory_profile_version" in source


def test_the_memory_migration_can_be_downgraded() -> None:
    downgrade = _run_migration("0007_memory_bitemporal", direction="downgrade")

    assert "memory_profile_version" in downgrade.dropped_tables
    dropped = set(downgrade.dropped_columns)
    assert ("memory_fact", "valid_from") in dropped
    assert ("memory_summary", "turn_range_end") in dropped


# -- 0008: chat and feedback ------------------------------------------------


def test_an_answer_is_stored_with_its_citations(chat_upgrade) -> None:
    """A citation that lives only in a trace cannot be read next to the answer."""
    added = chat_upgrade.added["message_index"]
    assert {"citations", "run_id"} <= added
    assert {"citations", "run_id"} <= set(MessageIndex.model_fields)


def test_the_model_declares_every_column_the_chat_migration_adds(chat_upgrade) -> None:
    assert chat_upgrade.added["message_index"] <= set(MessageIndex.model_fields)


def test_feedback_is_its_own_record_not_a_column(chat_upgrade) -> None:
    """The eval dataset is fed from here; overwriting a rating loses how an answer changed."""
    assert "feedback_entry" in chat_upgrade.created
    assert FeedbackEntry.__tablename__ == "feedback_entry"
    assert "rating" in FeedbackEntry.model_fields


def test_feedback_is_attributed_to_the_actor_who_rated(chat_upgrade) -> None:
    assert "user_id" in FeedbackEntry.model_fields


def test_feedback_gets_row_level_security(chat_upgrade) -> None:
    source = "\n".join(chat_upgrade.executed)
    assert '"feedback_entry" ENABLE ROW LEVEL SECURITY' in source
    assert "CREATE POLICY tenant_isolation_feedback_entry" in source


def test_the_chat_migration_can_be_downgraded() -> None:
    downgrade = _run_migration("0008_chat_feedback", direction="downgrade")

    assert "feedback_entry" in downgrade.dropped_tables
    assert ("message_index", "citations") in set(downgrade.dropped_columns)


def test_business_tables_are_not_created_in_postgres_by_models() -> None:
    """The `search` schema is for retrieval tables; control plane lives in `app`."""
    for model in (EvalCase, EvalRun, GraderValidation, RerankerArtifact):
        assert model.__table_args__[-1] == {"schema": "app"}
