"""Startup version gate probes; these do not execute PostgreSQL migrations."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ragos.core.config import Settings
from ragos.storage.db import REQUIRED_SCHEMA_REVISION, require_migrated_schema
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError


class RevisionEngine:
    def __init__(self, revisions=None, error=None):
        self.revisions = revisions
        self.error = error
        self.statements = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        if self.error:
            raise self.error
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.revisions))


@pytest.mark.parametrize(
    "revisions", [[], ["0009_fail_closed_rls"], ["future"], [REQUIRED_SCHEMA_REVISION, "other"]]
)
def test_missing_stale_future_or_branched_schema_refuses_startup(revisions):
    engine = RevisionEngine(revisions)
    with pytest.raises(RuntimeError, match="does not match"):
        require_migrated_schema(engine, Settings())
    assert len(engine.statements) == 1
    assert engine.statements[0].startswith("SELECT ")


def test_current_schema_is_read_only_and_schema_identifier_is_quoted():
    engine = RevisionEngine([REQUIRED_SCHEMA_REVISION])
    require_migrated_schema(engine, Settings(pg_app_schema='app"; SELECT secret; --'))
    assert '"app""; SELECT secret; --".alembic_version' in engine.statements[0]


def test_database_error_does_not_leak_connection_details():
    engine = RevisionEngine(error=SQLAlchemyError("private path and password"))
    with pytest.raises(RuntimeError, match="schema unavailable") as failure:
        require_migrated_schema(engine, Settings())
    assert "password" not in str(failure.value)
    assert failure.value.__suppress_context__
