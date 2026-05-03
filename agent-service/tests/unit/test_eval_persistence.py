"""Unit tests for eval-runs persistence.

The runner's exit code is decided by in-memory outcomes, but the
``eval_runs`` table is what the trend-tracking dashboards (PR 24+) read.
These tests pin two trust-relevant properties:

* The writer fails open: a DB error logs and skips, never raises into
  the runner. Eval persistence is observability, not the trust surface.
* Both observed and expected payloads are persisted so a future
  regression query needs no joins back to the case file (which may have
  changed by then).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from clinical_copilot.db.base import Base
from clinical_copilot.db.models import EvalRun
from tests.eval.harness import (
    CaseFailure,
    CaseOutcome,
    EvalCase,
    Expectation,
    Session,
)
from tests.eval.persistence import (
    NullEvalRunWriter,
    SqlEvalRunWriter,
    writer_from_database_url,
)


def _outcome(
    case_id: str,
    *,
    category: str = "happy_path",
    failures: tuple[CaseFailure, ...] = (),
    raw_response: dict[str, Any] | None = None,
    transport_error: str | None = None,
) -> CaseOutcome:
    case = EvalCase.model_validate(
        {
            "id": case_id,
            "category": category,
            "description": "synthetic",
            "query": "q",
            "session": Session(
                user_id="u",
                role="physician",
                patient_id="101",
                scopes=["system/Condition.read"],
            ).model_dump(),
            "expect": Expectation(
                abstention_state_in=[None],
                any_source_id_prefix=["Condition/"],
            ).model_dump(),
        }
    )
    return CaseOutcome(
        case=case,
        failures=failures,
        raw_response=raw_response,
        transport_error=transport_error,
    )


@pytest.fixture
def in_memory_factory() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def test_writer_persists_one_row_per_outcome(in_memory_factory: sessionmaker) -> None:
    writer = SqlEvalRunWriter(in_memory_factory)
    outcomes = [
        _outcome("happy_path/01", raw_response={"abstention": None, "tool_results": []}),
        _outcome(
            "missing_data/01",
            category="missing_data",
            failures=(CaseFailure(reason="bad state"),),
        ),
    ]
    persisted = writer.write(outcomes, run_id="run-abc")
    assert persisted == 2

    with in_memory_factory() as session:
        rows = list(session.execute(select(EvalRun).order_by(EvalRun.case_id)).scalars())
    assert [r.case_id for r in rows] == ["happy_path/01", "missing_data/01"]
    assert [r.suite for r in rows] == ["happy_path", "missing_data"]
    assert [r.passed for r in rows] == [True, False]
    assert all(r.run_id == "run-abc" for r in rows)


def test_writer_observed_includes_response_and_failures(in_memory_factory: sessionmaker) -> None:
    writer = SqlEvalRunWriter(in_memory_factory)
    outcomes = [
        _outcome(
            "happy_path/02",
            failures=(CaseFailure(reason="missing prefix"),),
            raw_response={"abstention": None, "tool_results": [{"records": []}]},
        ),
    ]
    writer.write(outcomes, run_id="run-xyz")

    with in_memory_factory() as session:
        row = session.execute(select(EvalRun)).scalar_one()
    observed = json.loads(row.observed)
    assert observed["passed"] is False
    assert observed["failures"] == ["missing prefix"]
    assert observed["response"]["tool_results"][0]["records"] == []


def test_writer_observed_includes_transport_error(in_memory_factory: sessionmaker) -> None:
    """A transport error means there's no response body — the row still
    needs to record *why* the case didn't run so the dashboard can
    distinguish 'agent broke' from 'assertion failed'."""

    writer = SqlEvalRunWriter(in_memory_factory)
    outcomes = [_outcome("ambiguous/01", category="ambiguous", transport_error="HTTP 502")]
    writer.write(outcomes, run_id="run-net")

    with in_memory_factory() as session:
        row = session.execute(select(EvalRun)).scalar_one()
    observed = json.loads(row.observed)
    assert observed["transport_error"] == "HTTP 502"
    assert "response" not in observed


def test_writer_expected_persists_full_expectation(in_memory_factory: sessionmaker) -> None:
    writer = SqlEvalRunWriter(in_memory_factory)
    writer.write([_outcome("happy_path/03")], run_id="run-exp")

    with in_memory_factory() as session:
        row = session.execute(select(EvalRun)).scalar_one()
    expected = json.loads(row.expected)
    assert expected == {
        "abstention_state_in": [None],
        "any_source_id_prefix": ["Condition/"],
        "any_prose_keyword_ci": None,
        "forbidden_source_id_regex": None,
        "forbidden_prose_regex_ci": None,
    }


def test_writer_fails_open_on_db_error(
    in_memory_factory: sessionmaker, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken sessionmaker must not propagate — the runner stays
    decided by the in-memory outcomes."""

    class _BoomSession:
        def __enter__(self) -> "_BoomSession":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def add_all(self, _rows: Any) -> None:
            raise RuntimeError("db is down")

        def commit(self) -> None:
            raise RuntimeError("never reached")

    def _factory() -> _BoomSession:
        return _BoomSession()

    writer = SqlEvalRunWriter(_factory)  # type: ignore[arg-type]
    persisted = writer.write([_outcome("happy_path/04")], run_id="run-boom")
    assert persisted == 0
    captured = capsys.readouterr()
    assert "eval_runs persistence skipped" in captured.err
    assert "run-boom" in captured.err


def test_writer_no_outcomes_writes_nothing(in_memory_factory: sessionmaker) -> None:
    writer = SqlEvalRunWriter(in_memory_factory)
    assert writer.write([], run_id="run-empty") == 0


def test_null_writer_returns_zero() -> None:
    assert NullEvalRunWriter().write([_outcome("x/y")], run_id="run-null") == 0


def test_writer_factory_returns_null_when_url_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = writer_from_database_url(None)
    assert isinstance(writer, NullEvalRunWriter)
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_writer_factory_returns_sql_writer_for_sqlite(tmp_path: Any) -> None:
    db_path = tmp_path / "agent.db"
    writer = writer_from_database_url(f"sqlite:///{db_path}")
    assert isinstance(writer, SqlEvalRunWriter)
