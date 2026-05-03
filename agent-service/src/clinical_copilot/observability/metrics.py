"""Internal metrics writer + summary aggregator (PR 21).

ARCHITECTURE §8.1 "beyond the minimum". One row per ``/api/agent/query`` is
appended to ``request_outcomes`` from the orchestrator (fail-open: a DB
hiccup in the observability path must not break the clinician answer). The
``summarize`` aggregation runs synchronously inside the metrics route — the
service has no scheduler today, and adding APScheduler for one rollup is a
new failure mode (drift, deploy semantics, thread leaks). Any external
poller (Railway healthcheck, future warm-keep cron from PR 27, the Daily
Brief admin tab) can scrape ``GET /api/agent/internal/metrics`` and play
the role of the cron.

Audit-log completeness drift (the spec's "background job, asserts every PHI
access has an audit row") is computed at scrape time as
``Σ tool_calls (in window) - count(audit_log SUCCESS in window)``. By
construction (PR 19's fail-closed audit writer) those should be equal —
non-zero drift means a real integrity problem. Comparing one outcome row
to one audit row would be wrong: a single chat turn fans out to several
FHIR reads, each with its own SUCCESS audit entry.

Window semantics. Every aggregation clamps to ``MAX_WINDOW`` (24h) so a
malformed query string can't scan the whole table. The cache hit rate is
intentionally cumulative-since-startup, not windowed — a per-lookup row
table would double the write traffic for a metric the dashboard doesn't
need windowed.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from clinical_copilot.db.models import AuditLog, RequestOutcome
from clinical_copilot.logging import get_logger
from clinical_copilot.tools.records import FlagRecord, ToolResult
from clinical_copilot.verification.abstention import Abstention, AbstentionState

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker

    from clinical_copilot.discrepancy.cache import DiscrepancyCache
    from clinical_copilot.orchestrator.lanes import Lane

logger = get_logger(__name__)

MAX_WINDOW = timedelta(hours=24)
DEFAULT_WINDOW = timedelta(hours=1)

# Headline buckets surfaced as ``state`` on a request_outcomes row. Distinct
# from :class:`AbstentionState` (which is finer-grained and lives on
# ``abstention_reason``) because the dashboard SLO panel wants three buckets,
# not four — operators reading "abstain rate spiked" first need to know
# whether the model is declining cleanly or the platform is failing.
STATE_VERIFIED = "verified"
STATE_ABSTAINED = "abstained"
STATE_FAILED = "failed"

# Mapping from precise abstention state to headline bucket.
# - NO_DATA / VERIFICATION_FAILED → semantic abstain (model declined)
# - TOOL_FAILURE / UNAUTHORIZED → operational/security failure (blocks the answer)
_BUCKET_FOR_ABSTENTION: dict[AbstentionState, str] = {
    AbstentionState.NO_DATA: STATE_ABSTAINED,
    AbstentionState.VERIFICATION_FAILED: STATE_ABSTAINED,
    AbstentionState.TOOL_FAILURE: STATE_FAILED,
    AbstentionState.UNAUTHORIZED: STATE_FAILED,
}


def classify_state(abstention: Abstention | None) -> tuple[str, str | None]:
    """Map an :class:`AgentResponse`'s abstention to (state, abstention_reason).

    ``abstention=None`` → ``("verified", None)``. Otherwise return the
    headline bucket and the precise abstention-state string (e.g. "NO_DATA").
    """

    if abstention is None:
        return STATE_VERIFIED, None
    return _BUCKET_FOR_ABSTENTION[abstention.state], abstention.state.value


def collect_fired_rule_ids(tool_results: Iterable[ToolResult]) -> list[str]:
    """Return the deduped union of ``rule_id`` across every FlagRecord seen.

    Order-preserving (first occurrence wins) so the JSON column is stable
    across replays of the same trace — useful when joining outcomes against
    eval re-runs.
    """

    seen: dict[str, None] = {}
    for tool_result in tool_results:
        for record in tool_result.records:
            if isinstance(record, FlagRecord):
                seen.setdefault(record.rule_id, None)
    return list(seen.keys())


@dataclass(frozen=True, slots=True)
class _FailedWriteCounter:
    """Process-local counter for outcomes the writer dropped fail-open.

    Surfaced in ``summarize`` so a dashboard can flag "metrics writer is
    silently dropping rows" before the operator notices the drift in the
    other charts. Held as a module global because the writer is constructed
    per-app and tests reset by re-importing (or calling :func:`reset`).
    """

    lock: threading.Lock
    value: list[int]


_FAILED_WRITES = _FailedWriteCounter(lock=threading.Lock(), value=[0])


def _bump_failed_writes() -> None:
    with _FAILED_WRITES.lock:
        _FAILED_WRITES.value[0] += 1


def _read_failed_writes() -> int:
    with _FAILED_WRITES.lock:
        return _FAILED_WRITES.value[0]


def reset_failed_writes() -> None:
    """Test helper — zero the process-local counter."""

    with _FAILED_WRITES.lock:
        _FAILED_WRITES.value[0] = 0


@dataclass(frozen=True, slots=True)
class _OutcomeAggregate:
    """Internal narrow projection of ``request_outcomes`` rows.

    Keeps :meth:`MetricsService.summarize` and :func:`_completeness`
    typed against concrete fields rather than ``dict[str, object]``,
    which would force ``int(...)`` casts at every call site and lose
    mypy's protection on the completeness math.
    """

    total: int
    tool_calls: int
    state_counts: dict[str, int]
    abstention_counts: dict[str, int]
    rule_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """Inputs to :meth:`MetricsService.record`.

    Frozen so the orchestrator can hand the writer a value object without
    fearing mutation between collection and commit.
    """

    request_id: str
    lane: str
    state: str
    abstention_reason: str | None
    tool_calls: int
    fired_rule_ids: list[str]


class MetricsService:
    """Owns request_outcomes I/O + summary aggregation.

    ``session_factory`` may be ``None`` on test paths that don't wire a DB —
    :meth:`record` becomes a logged no-op and :meth:`summarize` raises (the
    metrics route shouldn't even register without a DB; if it does, a 500
    is the right answer).
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[SqlSession] | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or _utcnow

    def record(self, outcome: OutcomeRecord) -> None:
        """Append one ``request_outcomes`` row. **Fail-open.**

        Any DB error is logged at warning level and bumps the process-local
        ``metrics_failed_writes`` counter — never raised. This is the
        deliberate inverse of :class:`AuditLogWriter`'s fail-closed contract:
        outcomes are observability, not the trust surface.
        """

        if self._session_factory is None:
            logger.debug(
                "metrics.record_skipped_no_db",
                request_id=outcome.request_id,
            )
            return

        row = RequestOutcome(
            request_id=outcome.request_id,
            lane=outcome.lane,
            state=outcome.state,
            abstention_reason=outcome.abstention_reason,
            tool_calls=outcome.tool_calls,
            fired_rule_ids=json.dumps(outcome.fired_rule_ids),
        )
        session = self._session_factory()
        try:
            session.add(row)
            session.commit()
        except Exception as exc:
            # Fail-open: log + bump local counter, never raise to the
            # clinician path. PR 19's audit writer is the place where DB
            # hiccups must surface as 5xx — outcomes are not.
            session.rollback()
            _bump_failed_writes()
            logger.warning(
                "metrics.record_failed",
                request_id=outcome.request_id,
                error=str(exc),
            )
        finally:
            session.close()

    def summarize(
        self,
        *,
        window: timedelta = DEFAULT_WINDOW,
        cache: DiscrepancyCache | None = None,
    ) -> dict[str, object]:
        """Return the JSON shape served by ``/api/agent/internal/metrics``.

        ``window`` is clamped to :data:`MAX_WINDOW` (24h) so a malformed
        query string can't scan the whole table. ``cache`` is optional —
        when omitted the cache panel is empty rather than the whole
        response failing; the metrics endpoint always passes one.
        """

        if self._session_factory is None:
            raise RuntimeError(
                "MetricsService.summarize requires a session_factory; "
                "this app was built without a DB"
            )

        clamped = min(window, MAX_WINDOW)
        if clamped <= timedelta(0):
            clamped = DEFAULT_WINDOW
        now = self._clock()
        since = now - clamped

        with self._session_factory() as session:
            outcomes = self._summarize_outcomes(session, since=since)
            audit = self._summarize_audit(session, since=since)

        completeness = _completeness(
            expected_audit_rows=outcomes.tool_calls,
            audit=audit,
        )
        cache_panel = _cache_panel(cache) if cache is not None else {}

        return {
            "window_seconds": int(clamped.total_seconds()),
            "as_of": now.isoformat(),
            "verification_outcome_rate": outcomes.state_counts,
            "abstention_state_distribution": outcomes.abstention_counts,
            "rbac_denial_rate": _rbac_rate(audit),
            "discrepancy_flag_distribution": outcomes.rule_counts,
            "cache": cache_panel,
            "audit_completeness": completeness,
            "metrics_failed_writes_since_startup": _read_failed_writes(),
            "totals": {
                "request_count": outcomes.total,
                "tool_calls": outcomes.tool_calls,
                "audit_success_count": audit["success"],
                "audit_unauthorized_count": audit["unauthorized"],
            },
        }

    def _summarize_outcomes(
        self,
        session: SqlSession,
        *,
        since: datetime,
    ) -> _OutcomeAggregate:
        rows = session.execute(
            select(
                RequestOutcome.state,
                RequestOutcome.abstention_reason,
                RequestOutcome.tool_calls,
                RequestOutcome.fired_rule_ids,
            ).where(RequestOutcome.ts >= since),
        ).all()

        state_counts: Counter[str] = Counter()
        abstention_counts: Counter[str] = Counter()
        rule_counts: Counter[str] = Counter()
        tool_calls_total = 0

        for state, reason, tool_calls, fired_json in rows:
            state_counts[state] += 1
            if reason is not None:
                abstention_counts[reason] += 1
            tool_calls_total += int(tool_calls or 0)
            for rule_id in _decode_rule_ids(fired_json):
                rule_counts[rule_id] += 1

        return _OutcomeAggregate(
            total=len(rows),
            tool_calls=tool_calls_total,
            state_counts=dict(state_counts),
            abstention_counts=dict(abstention_counts),
            rule_counts=dict(rule_counts),
        )

    def _summarize_audit(
        self,
        session: SqlSession,
        *,
        since: datetime,
    ) -> dict[str, int]:
        rows = session.execute(
            select(AuditLog.action, func.count())
            .where(AuditLog.ts >= since)
            .group_by(AuditLog.action),
        ).all()
        by_action: dict[str, int] = {action: int(count) for action, count in rows}
        return {
            "success": by_action.get("SUCCESS", 0),
            "unauthorized": by_action.get("UNAUTHORIZED", 0),
            "total": sum(by_action.values()),
        }


def _decode_rule_ids(payload: str | None) -> list[str]:
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str)]


def _rbac_rate(audit: dict[str, int]) -> float | None:
    """UNAUTHORIZED rows divided by all audit rows in the window.

    ``None`` when the window is empty — the route renders that as ``null``,
    which the dashboard distinguishes from ``0.0`` ("no denials, but
    traffic happened"). Without this distinction a fresh deploy looks
    indistinguishable from a healthy steady state.
    """

    if audit["total"] == 0:
        return None
    return audit["unauthorized"] / audit["total"]


def _completeness(
    *,
    expected_audit_rows: int,
    audit: dict[str, int],
) -> dict[str, object]:
    """Drift between ``Σ tool_calls`` and audit SUCCESS rows in the window.

    Should be 0 by construction (PR 19's fail-closed writer guarantees a
    SUCCESS row per tool dispatch). Non-zero is real drift the operator
    needs to see — a row that managed to write the outcome row but failed
    the audit row would mean PR 19's contract was bypassed (impossible
    today, but the metric is the canary that catches a future regression).
    """

    observed = audit["success"]
    return {
        "expected_audit_rows": expected_audit_rows,
        "observed_audit_rows": observed,
        "drift": observed - expected_audit_rows,
        "ok": expected_audit_rows == observed,
    }


def _cache_panel(cache: DiscrepancyCache) -> dict[str, object]:
    counters = cache.snapshot_counters()
    return {
        "hits_memory_since_startup": counters.hits_memory,
        "hits_durable_since_startup": counters.hits_durable,
        "misses_since_startup": counters.misses,
        "samples_since_startup": counters.total,
        "hit_rate_since_startup": counters.hit_rate,
    }


def build_outcome(
    *,
    request_id: str,
    lane: Lane,
    abstention: Abstention | None,
    tool_results: Iterable[ToolResult],
    tool_calls: int,
) -> OutcomeRecord:
    """Convenience builder used by the orchestrator.

    Centralised so the bucket-mapping logic (``classify_state``) and the
    flag-id collection happen in one place — the orchestrator stays focused
    on the run loop and the metrics module owns the metric shape.
    """

    state, reason = classify_state(abstention)
    return OutcomeRecord(
        request_id=request_id,
        lane=lane.value,
        state=state,
        abstention_reason=reason,
        tool_calls=tool_calls,
        fired_rule_ids=collect_fired_rule_ids(tool_results),
    )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
