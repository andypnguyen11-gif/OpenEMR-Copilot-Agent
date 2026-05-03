"""request_outcomes: per-request observability row

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-02

Backs the internal metrics endpoint (PR 21). One row per ``/api/agent/query``
call, written fail-open from :class:`Orchestrator.run` after the response is
built. Aggregations powering the dashboard are window-scoped (last hour /
last 24h) and run synchronously inside ``GET /api/agent/internal/metrics``;
the audit-log completeness drift is checked at scrape time, not by an
in-process scheduler — there isn't one yet, and adding APScheduler for one
metric is a new failure mode (drift, deploy semantics, thread leaks).

``tool_calls`` is the count of tool dispatches the orchestrator issued during
this request. Completeness math is ``Σ tool_calls (in window)`` against
``count(audit_log SUCCESS in window)``: by construction (PR 19's fail-closed
audit writer) those should be equal — non-zero drift means a real integrity
issue, not the natural fan-out of a single chat turn into several FHIR reads.

``fired_rule_ids`` is the deduped union of ``FlagRecord.rule_id`` across every
``ToolResult.records`` returned during the request. JSON-serialised array of
strings for the same reason ``discrepancy_cache.flags_json`` is opaque text:
adding a rule-id format quirk should not require a migration.

Column types stay portable (no JSONB, no UUID) so SQLite (dev) and Postgres
(prod) both work, matching the convention from ``0001_initial`` and
``0002_discrepancy_cache``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("lane", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("abstention_reason", sa.String(length=64), nullable=True),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fired_rule_ids", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_request_outcomes_ts", "request_outcomes", ["ts"])
    op.create_index("ix_request_outcomes_lane", "request_outcomes", ["lane"])
    op.create_index(
        "ix_request_outcomes_request_id",
        "request_outcomes",
        ["request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_outcomes_request_id", table_name="request_outcomes")
    op.drop_index("ix_request_outcomes_lane", table_name="request_outcomes")
    op.drop_index("ix_request_outcomes_ts", table_name="request_outcomes")
    op.drop_table("request_outcomes")
