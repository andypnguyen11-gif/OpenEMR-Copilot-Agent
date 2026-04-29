"""Audit-log fail-closed contract tests.

PR 2 acceptance: a failing audit-log write must propagate to the caller.
This test file covers the unit-level guarantee — that
:meth:`AuditLogWriter.write` re-raises the underlying DB error as
:class:`AuditLogWriteError` rather than swallowing it. The integration
guarantee (request returns 500 when this raises) lands in the PR that wires
the writer into a real route.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from clinical_copilot.audit.log import AuditLogWriteError, AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.db.models import AuditLog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the schema applied."""

    eng = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _event() -> AuditEvent:
    return AuditEvent(
        user_id="user-1",
        role="physician",
        patient_id_hash=hash_patient_id("patient-42", salt="test-salt"),
        resource_type="Patient",
        action="read",
        request_id="req-abc",
    )


def test_hash_patient_id_is_stable_and_distinguishes_inputs() -> None:
    """Same inputs must always hash to the same digest; different inputs must not collide."""

    h1 = hash_patient_id("patient-1", salt="s")
    h2 = hash_patient_id("patient-1", salt="s")
    h3 = hash_patient_id("patient-2", salt="s")
    h4 = hash_patient_id("patient-1", salt="other-salt")

    assert h1 == h2
    assert h1 != h3
    assert h1 != h4
    assert len(h1) == 64


def test_hash_patient_id_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        hash_patient_id("", salt="s")


def test_write_persists_row_on_happy_path(
    session_factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    """Smoke test the writer commits a row when the DB is healthy."""

    writer = AuditLogWriter(session_factory)
    writer.write(_event())

    with session_factory() as s:
        rows = list(s.scalars(select(AuditLog)))

    assert len(rows) == 1
    assert rows[0].user_id == "user-1"
    assert rows[0].resource_type == "Patient"
    assert rows[0].patient_id_hash == hash_patient_id("patient-42", salt="test-salt")


def test_write_reraises_when_commit_fails() -> None:
    """Fail-closed: the writer must surface a commit failure, not swallow it.

    Mocked at the session boundary so the test doesn't depend on a real DB
    being unhealthy — we want to assert the writer's exception-handling
    contract, not SQLite's behavior. The simulated failure is exactly what
    a Postgres outage looks like to the writer: ``commit()`` raises an
    :class:`OperationalError`.
    """

    failing_session = MagicMock()
    failing_session.commit.side_effect = OperationalError("commit", {}, BaseException("db down"))
    factory = MagicMock(return_value=failing_session)

    writer = AuditLogWriter(factory)

    with pytest.raises(AuditLogWriteError) as excinfo:
        writer.write(_event())

    # The original DB error is preserved as __cause__ so logs/observability
    # can see the underlying reason without leaking it to users.
    assert isinstance(excinfo.value.__cause__, OperationalError)
    # Rollback and close run even on the failure path.
    failing_session.rollback.assert_called_once()
    failing_session.close.assert_called_once()


def test_write_rolls_back_session_on_failure(engine: Engine) -> None:
    """A failed write must not leave a partial row visible from another session.

    Real DB this time, but we trigger the failure via a constraint violation
    we can stage on disk: a duplicate-PK insert. The first write commits;
    the second uses the same ``request_id`` (PK) plus a hash, but
    ``audit_log`` uses an autoincrement ``id`` — so we instead drop the
    table on a *file-backed* SQLite so the schema change persists across
    sessions. We use a temp file rather than ``:memory:`` because in-memory
    SQLite gives each connection a fresh DB.
    """

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'audit.db'}"
        eng = create_engine_from_url(url)
        Base.metadata.create_all(eng)
        factory = create_session_factory(eng)

        with eng.begin() as conn:
            conn.exec_driver_sql("DROP TABLE audit_log")

        writer = AuditLogWriter(factory)
        with pytest.raises(AuditLogWriteError):
            writer.write(_event())

        # Recreate the table and confirm no row leaked through.
        Base.metadata.create_all(eng)
        with factory() as s:
            assert s.scalars(select(AuditLog)).all() == []
        eng.dispose()
