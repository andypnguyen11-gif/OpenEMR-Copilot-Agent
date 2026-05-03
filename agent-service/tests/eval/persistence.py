"""Eval-runs persistence writer.

Inserts one :class:`clinical_copilot.db.models.EvalRun` row per
``(run_id, case_id)`` pair so the pre-merge gate (PR 24) and trend
dashboards can query historical pass-rates without re-running the
suite.

Two design choices worth flagging:

* **Fail-open writes.** A DB hiccup logs to stderr and skips
  persistence; the runner's exit code stays a function of the in-memory
  outcomes only. Eval persistence is observability, not the trust
  surface — losing a row drops a data point, not an audit trail. Mirrors
  the same posture as ``request_outcomes`` (PR 21).
* **Optional database URL.** When ``DATABASE_URL`` is unset the runner
  must still execute (developers run ``make eval`` against a deployed
  agent without a local Postgres). :class:`NullEvalRunWriter` is the
  no-op fallback.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.db.models import EvalRun

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.orm import Session as SqlaSession

    from tests.eval.harness import CaseOutcome


class EvalRunWriter(Protocol):
    """Persist outcomes for one eval invocation."""

    def write(self, outcomes: list[CaseOutcome], *, run_id: str) -> int:
        """Write outcomes; return the number of rows persisted."""


class NullEvalRunWriter:
    """No-op writer used when no ``DATABASE_URL`` is configured."""

    def write(self, outcomes: list[CaseOutcome], *, run_id: str) -> int:
        return 0


class SqlEvalRunWriter:
    """SQLAlchemy-backed writer.

    Constructed with a sessionmaker so tests can inject one bound to an
    in-memory SQLite engine without spinning up a connection pool.
    """

    def __init__(self, session_factory: sessionmaker[SqlaSession]) -> None:
        self._session_factory = session_factory

    def write(self, outcomes: list[CaseOutcome], *, run_id: str) -> int:
        if not outcomes:
            return 0
        rows = [_outcome_to_row(o, run_id=run_id) for o in outcomes]
        try:
            with self._session_factory() as session:
                session.add_all(rows)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
            print(
                f"eval_runs persistence skipped (run_id={run_id}): {exc!s}",
                file=sys.stderr,
            )
            return 0
        return len(rows)


def writer_from_database_url(url: str | None) -> EvalRunWriter:
    """Build the appropriate writer for ``url``.

    ``None`` or an empty string returns the null writer with a single
    stderr line so the operator notices the suite ran without trend
    persistence.
    """

    if not url:
        print(
            "DATABASE_URL not set — eval results will not be persisted to eval_runs.",
            file=sys.stderr,
        )
        return NullEvalRunWriter()
    engine = create_engine_from_url(url)
    factory = create_session_factory(engine)
    return SqlEvalRunWriter(factory)


def _outcome_to_row(outcome: CaseOutcome, *, run_id: str) -> EvalRun:
    """Project a :class:`CaseOutcome` onto an :class:`EvalRun` row.

    ``observed`` carries the response body when the agent answered, the
    transport error string when it didn't, and the failure-reason list
    on assertion failures. ``expected`` is the case's full
    :class:`Expectation` — keeping both sides on the row means a
    historical regression query needs no joins.
    """
    observed_payload: dict[str, object] = {
        "passed": outcome.passed,
        "failures": [f.reason for f in outcome.failures],
    }
    if outcome.transport_error is not None:
        observed_payload["transport_error"] = outcome.transport_error
    if outcome.raw_response is not None:
        observed_payload["response"] = outcome.raw_response

    return EvalRun(
        run_id=run_id,
        case_id=outcome.case.case_id,
        suite=outcome.case.category,
        passed=outcome.passed,
        observed=json.dumps(observed_payload, default=_json_default),
        expected=outcome.case.expect.model_dump_json(),
    )


def _json_default(value: object) -> str:
    """Coerce non-JSON-native values (e.g. ``datetime``) to strings."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return repr(value)
