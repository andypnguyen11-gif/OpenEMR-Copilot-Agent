"""Per-request trace writer (PR W2-04).

Sibling of :mod:`clinical_copilot.observability.metrics`. One row per
agent request lands in ``agent_traces`` from the orchestrator (slow- and
fast-lane queries) and from the document-ingest entry point — same
table, different shapes:

* ``/api/agent/query`` writes a row with ``retrieval_hits`` populated
  (chunk count from the evidence retriever, ``NULL`` on chart-only or
  fast-lane turns) and ``extraction_confidence`` ``NULL``.
* ``/api/agent/internal/ingest`` writes a row with
  ``extraction_confidence`` populated (mean per-field confidence from
  the document extractor) and ``retrieval_hits`` ``NULL``.

Same fail-open contract as :class:`MetricsService`: any DB error is
logged at warning and bumps a process-local counter; the writer never
raises into the clinician path. The audit-log writer (PR 19) is the one
that must stay fail-closed — traces are observability, not the trust
surface, so a DB hiccup here is a missing dot on a chart, not a
compliance violation.

``user_id`` is the JWT-verified clinician id (``ClinicianClaims.user_id``).
We do NOT hash it before storing — unlike ``audit_log.patient_id_hash``,
the user id isn't PHI; it's the same string the gateway already prints
in request logs and joins are needed across this table and ``audit_log``
without a salted-hash round-trip.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clinical_copilot.db.models import AgentTrace
from clinical_copilot.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _FailedWriteCounter:
    """Process-local counter for trace rows the writer dropped fail-open.

    Held as a module global because :class:`TracesService` is constructed
    per-app; tests reset by calling :func:`reset_failed_writes`. Mirror
    of the equivalent counter in :mod:`metrics` — the two are independent
    so a shared DB outage shows up on both panels (and the operator can
    tell whether the audit panel is affected too by checking PR 19's
    fail-closed counter on the `audit_log` side).
    """

    lock: threading.Lock
    value: list[int]


_FAILED_WRITES = _FailedWriteCounter(lock=threading.Lock(), value=[0])


def _bump_failed_writes() -> None:
    with _FAILED_WRITES.lock:
        _FAILED_WRITES.value[0] += 1


def read_failed_writes() -> int:
    """Public read of the process-local fail-open counter.

    Surfaced for the metrics endpoint and for tests that assert the
    fail-open path was actually taken. Read-only — incrementing happens
    inside :meth:`TracesService.record` and is not part of the public
    API.
    """

    with _FAILED_WRITES.lock:
        return _FAILED_WRITES.value[0]


def reset_failed_writes() -> None:
    """Test helper — zero the process-local counter."""

    with _FAILED_WRITES.lock:
        _FAILED_WRITES.value[0] = 0


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Per-request token totals aggregated across every Anthropic call.

    Built by summing each ``response.usage`` (or :class:`LlmTurn`'s
    ``input_tokens`` / ``output_tokens``) seen during a single agent
    request. Surfaced on :class:`SupervisorResponse` and the v1
    orchestrator's run-loop locals so :meth:`TracesService.record` can
    write one row per request without re-walking the message history.

    ``+`` is defined so call sites can fold partial totals (planner +
    synthesizer + critic + rerank) without a manual tuple-unpack.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """Inputs to :meth:`TracesService.record`.

    Frozen so the call site can build the value object once and hand it
    off to the writer without fearing mutation between collection and
    commit. ``retrieval_hits`` and ``extraction_confidence`` are
    independently nullable (see :mod:`db.models` and the 0004 migration
    docstring) — the call site nulls whichever doesn't apply for this
    request shape.
    """

    request_id: str
    user_id: str
    role: str
    lane: str
    latency_ms: int
    token_in: int
    token_out: int
    model_tier: str
    retrieval_hits: int | None = None
    extraction_confidence: float | None = None


class TracesService:
    """Owns ``agent_traces`` writes. **Fail-open.**

    ``session_factory`` may be ``None`` on test paths that don't wire a
    DB — :meth:`record` becomes a logged no-op. Production always wires
    one. The deliberate inverse of :class:`AuditLogWriter`'s fail-closed
    contract: traces are observability, not the trust surface, so a DB
    hiccup here logs and increments the local counter but never
    propagates into the clinician path.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[SqlSession] | None,
    ) -> None:
        self._session_factory = session_factory

    def record(self, trace: TraceRecord) -> None:
        """Append one ``agent_traces`` row. Fail-open."""

        if self._session_factory is None:
            logger.debug(
                "traces.record_skipped_no_db",
                request_id=trace.request_id,
            )
            return

        row = AgentTrace(
            request_id=trace.request_id,
            user_id=trace.user_id,
            role=trace.role,
            lane=trace.lane,
            latency_ms=trace.latency_ms,
            token_in=trace.token_in,
            token_out=trace.token_out,
            model_tier=trace.model_tier,
            retrieval_hits=trace.retrieval_hits,
            extraction_confidence=trace.extraction_confidence,
        )
        session = self._session_factory()
        try:
            session.add(row)
            session.commit()
        except Exception as exc:
            session.rollback()
            _bump_failed_writes()
            logger.warning(
                "traces.record_failed",
                request_id=trace.request_id,
                error=str(exc),
            )
        finally:
            session.close()
