"""SQLAlchemy ORM models for the agent metadata DB.

Three tables, each per ARCHITECTURE §8:

* ``agent_traces`` — one row per agent request. Powers offline analysis when
  LangSmith is unavailable or when joining trace data with eval results.
* ``eval_runs`` — one row per (run, case) pair from the eval harness. The
  pre-merge gate reads the latest run; passing rows are not deleted, so we can
  trend pass-rate over time.
* ``audit_log`` — append-only HIPAA-relevant log. Every PHI access writes one
  row. Patient IDs are stored only as HMAC-SHA256 hashes (see
  :mod:`clinical_copilot.audit.log`); raw IDs never land here.

The audit log is the load-bearing table; the other two are operational.
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
