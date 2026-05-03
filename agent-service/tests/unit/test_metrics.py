"""Unit tests for the PR 21 metrics module.

Covers the bucket-mapping + flag-id collection helpers, the fail-open
contract on :meth:`MetricsService.record`, and the window-clamped
aggregations served by :meth:`MetricsService.summarize`. The integration
test (``tests/integration/test_metrics_route.py``) is the cross-PR pin
that the orchestrator actually writes the row the metrics endpoint reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.db.models import RequestOutcome
from clinical_copilot.observability.metrics import (
    DEFAULT_WINDOW,
    MAX_WINDOW,
    MetricsService,
    OutcomeRecord,
    build_outcome,
    classify_state,
    collect_fired_rule_ids,
    reset_failed_writes,
)
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.tools.records import (
    FlagRecord,
    ProblemRecord,
    ToolResult,
)
from clinical_copilot.verification.abstention import Abstention, AbstentionState

if TYPE_CHECKING:
    pass


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
def _reset_failed_writes() -> Iterator[None]:
    """The fail-open counter is module-global. Zero it between tests so a
    failure-mode test isn't polluted by a neighbour's increment.
    """

    reset_failed_writes()
    yield
    reset_failed_writes()


def _outcome(
    *,
    request_id: str = "req-1",
    lane: str = "slow",
    state: str = "verified",
    abstention_reason: str | None = None,
    tool_calls: int = 0,
    fired_rule_ids: list[str] | None = None,
) -> OutcomeRecord:
    return OutcomeRecord(
        request_id=request_id,
        lane=lane,
        state=state,
        abstention_reason=abstention_reason,
        tool_calls=tool_calls,
        fired_rule_ids=list(fired_rule_ids or []),
    )


# ---------- classify_state ----------


def test_classify_state_returns_verified_for_no_abstention() -> None:
    assert classify_state(None) == ("verified", None)


@pytest.mark.parametrize(
    ("state", "expected_bucket"),
    [
        (AbstentionState.NO_DATA, "abstained"),
        (AbstentionState.VERIFICATION_FAILED, "abstained"),
        (AbstentionState.TOOL_FAILURE, "failed"),
        (AbstentionState.UNAUTHORIZED, "failed"),
    ],
)
def test_classify_state_bucket_mapping(
    state: AbstentionState,
    expected_bucket: str,
) -> None:
    """Per PR 21 design: NO_DATA / VERIFICATION_FAILED → abstained
    (semantic decline); TOOL_FAILURE / UNAUTHORIZED → failed (operational
    or security failure that blocked the answer). The headline-bucket
    split is the dashboard SLO panel; the precise reason rides on the
    second tuple slot for the triage panel.
    """

    bucket, reason = classify_state(Abstention(state=state, reason="why"))
    assert bucket == expected_bucket
    assert reason == state.value


# ---------- collect_fired_rule_ids ----------


def _flag(rule_id: str, source_id: str = "src") -> FlagRecord:
    return FlagRecord(
        source_id=source_id,
        rule_id=rule_id,
        category="safety",
        rationale="r",
        referenced_source_ids=[],
    )


def _problem(source_id: str) -> ProblemRecord:
    return ProblemRecord(source_id=source_id, code="c", display="d", status="active")


def _result(records: list[FlagRecord | ProblemRecord]) -> ToolResult:
    return ToolResult(
        tool_name="get_flags",
        patient_id="101",
        records=list(records),
    )


def test_collect_fired_rule_ids_dedupes_and_preserves_first_seen_order() -> None:
    results = [
        _result([_flag("rule-a", "s1"), _flag("rule-b", "s2")]),
        _result([_flag("rule-a", "s3"), _flag("rule-c", "s4")]),
    ]
    assert collect_fired_rule_ids(results) == ["rule-a", "rule-b", "rule-c"]


def test_collect_fired_rule_ids_ignores_non_flag_records() -> None:
    """Non-FlagRecord records (problems, meds, etc.) carry no rule_id and
    must not contribute to the distribution — otherwise the chart would
    conflate "rules that fired" with "every record we read."
    """

    results = [_result([_problem("p1"), _flag("rule-a")])]
    assert collect_fired_rule_ids(results) == ["rule-a"]


def test_collect_fired_rule_ids_returns_empty_list_for_empty_input() -> None:
    assert collect_fired_rule_ids([]) == []


# ---------- build_outcome ----------


def test_build_outcome_assembles_value_object_for_verified_run() -> None:
    outcome = build_outcome(
        request_id="req-build",
        lane=Lane.FAST,
        abstention=None,
        tool_results=[_result([_flag("r1")])],
        tool_calls=2,
    )
    assert outcome.request_id == "req-build"
    assert outcome.lane == "fast"
    assert outcome.state == "verified"
    assert outcome.abstention_reason is None
    assert outcome.tool_calls == 2
    assert outcome.fired_rule_ids == ["r1"]


# ---------- MetricsService.record ----------


def test_record_persists_row_on_happy_path(
    session_factory: sessionmaker[Session],
) -> None:
    service = MetricsService(session_factory=session_factory)
    service.record(
        _outcome(
            request_id="req-happy",
            lane="slow",
            state="verified",
            tool_calls=3,
            fired_rule_ids=["rule-a", "rule-b"],
        ),
    )

    with session_factory() as session:
        rows = list(session.scalars(select(RequestOutcome)))
    assert len(rows) == 1
    assert rows[0].request_id == "req-happy"
    assert rows[0].lane == "slow"
    assert rows[0].state == "verified"
    assert rows[0].abstention_reason is None
    assert rows[0].tool_calls == 3
    assert rows[0].fired_rule_ids == '["rule-a", "rule-b"]'


def test_record_is_fail_open_when_commit_fails() -> None:
    """Inverse of :class:`AuditLogWriter`'s fail-closed contract: a DB
    hiccup in the metrics writer must be logged + counted but **never**
    raised into the orchestrator. A regression that lets an exception
    propagate would break a clinician answer for an observability blip.
    """

    failing_session = MagicMock()
    failing_session.commit.side_effect = OperationalError(
        "commit", {}, BaseException("simulated outage")
    )
    factory = MagicMock(return_value=failing_session)

    service = MetricsService(session_factory=factory)
    # Must not raise.
    service.record(_outcome())

    failing_session.rollback.assert_called_once()
    failing_session.close.assert_called_once()


def test_record_no_op_when_session_factory_is_none() -> None:
    service = MetricsService(session_factory=None)
    service.record(_outcome())  # no DB; no exception


# ---------- MetricsService.summarize ----------


def _seed_outcome(
    session_factory: sessionmaker[Session],
    *,
    state: str,
    abstention_reason: str | None = None,
    tool_calls: int = 0,
    fired_rule_ids: str = "[]",
    ts: datetime | None = None,
    lane: str = "slow",
    request_id: str = "req",
) -> None:
    row = RequestOutcome(
        request_id=request_id,
        lane=lane,
        state=state,
        abstention_reason=abstention_reason,
        tool_calls=tool_calls,
        fired_rule_ids=fired_rule_ids,
    )
    if ts is not None:
        row.ts = ts
    with session_factory() as session:
        session.add(row)
        session.commit()


def _seed_audit(
    audit_writer: AuditLogWriter,
    *,
    action: str,
    request_id: str = "req",
) -> None:
    audit_writer.write(
        AuditEvent(
            user_id="u",
            role="physician",
            patient_id_hash=hash_patient_id("p", salt="s"),
            resource_type="get_problems",
            action=action,
            request_id=request_id,
        )
    )


def test_summarize_aggregates_state_buckets_and_rule_distribution(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_outcome(
        session_factory,
        state="verified",
        tool_calls=2,
        fired_rule_ids='["rule-a", "rule-b"]',
        request_id="r1",
    )
    _seed_outcome(
        session_factory,
        state="abstained",
        abstention_reason="NO_DATA",
        tool_calls=1,
        fired_rule_ids='["rule-a"]',
        request_id="r2",
    )
    _seed_outcome(
        session_factory,
        state="failed",
        abstention_reason="UNAUTHORIZED",
        tool_calls=0,
        fired_rule_ids="[]",
        request_id="r3",
    )

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize(window=timedelta(hours=1))

    assert summary["verification_outcome_rate"] == {
        "verified": 1,
        "abstained": 1,
        "failed": 1,
    }
    assert summary["abstention_state_distribution"] == {
        "NO_DATA": 1,
        "UNAUTHORIZED": 1,
    }
    assert summary["discrepancy_flag_distribution"] == {
        "rule-a": 2,
        "rule-b": 1,
    }
    totals = summary["totals"]
    assert isinstance(totals, dict)
    assert totals["request_count"] == 3
    assert totals["tool_calls"] == 3


def test_summarize_excludes_outcomes_outside_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    """Old rows must not pollute a 1-hour SLO panel — otherwise a
    historically bad day pins the rate forever and the operator can't
    tell whether the issue is current."""

    far_past = datetime.now(tz=UTC) - timedelta(hours=10)
    _seed_outcome(
        session_factory,
        state="failed",
        abstention_reason="TOOL_FAILURE",
        ts=far_past,
        request_id="old",
    )
    _seed_outcome(
        session_factory,
        state="verified",
        request_id="new",
    )

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize(window=timedelta(hours=1))
    totals = summary["totals"]
    assert isinstance(totals, dict)
    assert totals["request_count"] == 1
    assert summary["verification_outcome_rate"] == {"verified": 1}


def test_summarize_clamps_window_to_max(
    session_factory: sessionmaker[Session],
) -> None:
    """Even if a caller asks for a 30-day window, the response must
    declare ``window_seconds`` capped at MAX_WINDOW so the dashboard
    doesn't accidentally label a fresh-process snapshot as a long-term
    trend.
    """

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize(window=timedelta(days=30))
    assert summary["window_seconds"] == int(MAX_WINDOW.total_seconds())


def test_summarize_default_window_is_one_hour(
    session_factory: sessionmaker[Session],
) -> None:
    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()
    assert summary["window_seconds"] == int(DEFAULT_WINDOW.total_seconds())


def test_summarize_rbac_denial_rate_from_audit_log(
    session_factory: sessionmaker[Session],
) -> None:
    audit_writer = AuditLogWriter(session_factory=session_factory)
    for i in range(7):
        _seed_audit(audit_writer, action="SUCCESS", request_id=f"s-{i}")
    for i in range(3):
        _seed_audit(audit_writer, action="UNAUTHORIZED", request_id=f"u-{i}")

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()

    assert summary["rbac_denial_rate"] == pytest.approx(0.3)
    totals = summary["totals"]
    assert isinstance(totals, dict)
    assert totals["audit_success_count"] == 7
    assert totals["audit_unauthorized_count"] == 3


def test_summarize_rbac_denial_rate_is_none_when_window_is_empty(
    session_factory: sessionmaker[Session],
) -> None:
    """Distinct from 0.0: a fresh deploy with no traffic shouldn't look
    like a healthy steady state with zero denials. ``None`` lets the
    dashboard render "no data" instead of a misleading green tile."""

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()
    assert summary["rbac_denial_rate"] is None


def test_summarize_completeness_ok_when_tool_calls_match_audit_success(
    session_factory: sessionmaker[Session],
) -> None:
    audit_writer = AuditLogWriter(session_factory=session_factory)
    _seed_outcome(session_factory, state="verified", tool_calls=3, request_id="r1")
    _seed_outcome(session_factory, state="verified", tool_calls=2, request_id="r2")
    for i in range(5):
        _seed_audit(audit_writer, action="SUCCESS", request_id=f"s-{i}")

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()
    completeness = summary["audit_completeness"]
    assert isinstance(completeness, dict)
    assert completeness["expected_audit_rows"] == 5
    assert completeness["observed_audit_rows"] == 5
    assert completeness["drift"] == 0
    assert completeness["ok"] is True


def test_summarize_completeness_surfaces_drift_when_audit_lags(
    session_factory: sessionmaker[Session],
) -> None:
    """Under PR 19's fail-closed audit writer, ``Σ tool_calls`` should
    equal ``count(audit_log SUCCESS)``. A negative drift means the audit
    writer was bypassed somehow — the whole point of this metric is to
    surface that as a non-zero number the operator can see.
    """

    audit_writer = AuditLogWriter(session_factory=session_factory)
    _seed_outcome(session_factory, state="verified", tool_calls=4, request_id="r1")
    _seed_audit(audit_writer, action="SUCCESS", request_id="s1")

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()
    completeness = summary["audit_completeness"]
    assert isinstance(completeness, dict)
    assert completeness["expected_audit_rows"] == 4
    assert completeness["observed_audit_rows"] == 1
    assert completeness["drift"] == -3
    assert completeness["ok"] is False


def test_summarize_includes_cache_panel_when_cache_is_provided(
    session_factory: sessionmaker[Session],
) -> None:
    """The cache hit-rate field is intentionally cumulative-since-startup,
    not windowed — the panel exists to spot "the cache isn't doing its
    job," for which a cumulative figure with a ``samples`` denominator is
    enough.
    """

    cache = MagicMock()
    cache.snapshot_counters.return_value = MagicMock(
        hits_memory=10,
        hits_durable=5,
        misses=5,
        total=20,
        hit_rate=0.75,
    )

    service = MetricsService(session_factory=session_factory)
    summary = service.summarize(cache=cache)

    panel = summary["cache"]
    assert isinstance(panel, dict)
    assert panel["hits_memory_since_startup"] == 10
    assert panel["hits_durable_since_startup"] == 5
    assert panel["misses_since_startup"] == 5
    assert panel["samples_since_startup"] == 20
    assert panel["hit_rate_since_startup"] == 0.75


def test_summarize_cache_panel_empty_when_cache_omitted(
    session_factory: sessionmaker[Session],
) -> None:
    service = MetricsService(session_factory=session_factory)
    summary = service.summarize()
    assert summary["cache"] == {}


def test_summarize_raises_when_session_factory_missing() -> None:
    service = MetricsService(session_factory=None)
    with pytest.raises(RuntimeError, match="session_factory"):
        service.summarize()


def test_failed_write_counter_is_surfaced_in_summary(
    session_factory: sessionmaker[Session],
) -> None:
    """A regression that quietly drops outcome rows must not also
    quietly drop its own breadcrumb — the operator should see the
    counter rise even before the other charts notice the missing rows.
    """

    failing_factory = MagicMock(
        return_value=MagicMock(
            commit=MagicMock(
                side_effect=OperationalError("c", {}, BaseException("down")),
            ),
        ),
    )
    failing_service = MetricsService(session_factory=failing_factory)
    failing_service.record(_outcome())
    failing_service.record(_outcome())

    reader = MetricsService(session_factory=session_factory)
    summary = reader.summarize()
    assert summary["metrics_failed_writes_since_startup"] == 2
