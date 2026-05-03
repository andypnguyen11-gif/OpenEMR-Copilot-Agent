"""SQLAlchemy ORM models for the agent metadata DB.

Tables, per ARCHITECTURE §6 / §8:

* ``agent_traces`` — one row per agent request. Powers offline analysis when
  LangSmith is unavailable or when joining trace data with eval results.
* ``eval_runs`` — one row per (run, case) pair from the eval harness. The
  pre-merge gate reads the latest run; passing rows are not deleted, so we can
  trend pass-rate over time.
* ``audit_log`` — append-only HIPAA-relevant log. Every PHI access writes one
  row. Patient IDs are stored only as HMAC-SHA256 hashes (see
  :mod:`clinical_copilot.audit.log`); raw IDs never land here.
* ``discrepancy_cache`` — durable tier of the two-tier flag cache (PR 14).
  One row per patient_id; the JSON blob is the ``FlagRecord`` list the engine
  emitted last, with ``expires_at`` as the TTL boundary.
* ``request_outcomes`` — one row per ``/api/agent/query`` call, fail-open
  written from the orchestrator after the response is built. Backs the
  internal metrics endpoint (PR 21). Distinct from ``audit_log`` (which is
  fail-closed and HIPAA-relevant) — losing an outcome row is an observability
  blip, not an integrity violation.

The audit log is the load-bearing table; the others are operational.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from clinical_copilot.db.base import Base


class AgentTrace(Base):
    """One row per agent request (slow- or fast-lane).

    ``request_id`` is the primary key — it's the same correlation ID stamped on
    the request from the gateway, so traces, audit-log rows, and eval results
    can be joined on it without a surrogate key.
    """

    __tablename__ = "agent_traces"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    lane: Mapped[str] = mapped_column(String(8))
    latency_ms: Mapped[int] = mapped_column(Integer)
    token_in: Mapped[int] = mapped_column(Integer)
    token_out: Mapped[int] = mapped_column(Integer)
    model_tier: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class EvalRun(Base):
    """One row per (run, case) outcome from the eval harness.

    ``observed`` and ``expected`` are JSON-serialized text blobs — keeping them
    opaque to the schema means new eval shapes don't require migrations.
    """

    __tablename__ = "eval_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    suite: Mapped[str] = mapped_column(String(64), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    observed: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AuditLog(Base):
    """HIPAA-relevant access log — append-only.

    No update/delete operations are performed on this table by application
    code; row immutability is a property of the writer, not the schema (we do
    not enforce it via DB-level revoke-on-update because that complicates the
    Railway managed-Postgres flow). The fail-closed writer in
    :mod:`clinical_copilot.audit.log` guarantees that a row is committed
    *before* the request that triggered the PHI access is allowed to succeed.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    patient_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(64), index=True)


class RequestOutcome(Base):
    """One row per ``/api/agent/query`` call (PR 21).

    Written fail-open from :class:`Orchestrator.run` after the response is
    built — a DB hiccup here logs and increments a process-local counter
    but never raises into the clinician path. That posture is deliberately
    weaker than :class:`AuditLog` (PR 19): outcomes are observability;
    losing one drops a bucket in a chart, not an audit trail.

    ``state`` is the headline bucket on the dashboard: ``verified`` (response
    trusted, no abstention), ``abstained`` (model declined or verification
    rejected the draft — ``NO_DATA`` / ``VERIFICATION_FAILED``), or ``failed``
    (operational/security error that blocked the answer — ``TOOL_FAILURE`` /
    ``UNAUTHORIZED``). ``abstention_reason`` carries the precise
    :class:`AbstentionState` value when one was set, ``NULL`` on verified
    rows. The bucket-vs-reason split lets the metrics endpoint return both
    a coarse rate (for SLO panels) and a fine distribution (for triage)
    without joining anything else.

    ``tool_calls`` powers the audit-log completeness check: ``Σ tool_calls``
    in a window should equal ``count(audit_log SUCCESS)`` in the same window.
    Comparing one outcome row to one audit row would be wrong — a single
    chat turn fans out to several FHIR reads, each writing its own SUCCESS
    audit entry.

    ``fired_rule_ids`` is JSON-serialised text for the same reason
    ``flags_json`` is opaque on :class:`DiscrepancyCacheRow`: rule-id format
    changes shouldn't require a migration.
    """

    __tablename__ = "request_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    lane: Mapped[str] = mapped_column(String(8), index=True)
    state: Mapped[str] = mapped_column(String(32))
    abstention_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fired_rule_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class DiscrepancyCacheRow(Base):
    """Durable tier of the two-tier flag cache (PR 14).

    Sits behind :class:`clinical_copilot.discrepancy.cache.DiscrepancyCache`;
    nothing else touches this table. ``flags_json`` is the JSON-serialized
    ``FlagRecord`` list the engine emitted, kept opaque so adding fields to
    ``FlagRecord`` doesn't require a migration. ``expires_at`` is the TTL
    boundary — a row whose ``expires_at`` is in the past is treated as
    absent on read and overwritten on the next recompute.
    """

    __tablename__ = "discrepancy_cache"

    patient_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    flags_json: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
