"""discrepancy_cache: durable tier of the two-tier flag cache

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-02

Backs ``discrepancy/cache.py`` (PR 14). One row per patient_id; the
``flags_json`` blob is the JSON-serialized ``FlagRecord`` list the engine
emitted, and ``expires_at`` is the wall-clock TTL boundary. SQLite (dev)
and Postgres (prod) both work — the column types stay portable (no JSONB,
no UUID) the way ``0001_initial`` did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discrepancy_cache",
        sa.Column("patient_id", sa.String(length=64), primary_key=True),
        sa.Column("flags_json", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_discrepancy_cache_expires_at",
        "discrepancy_cache",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_discrepancy_cache_expires_at", table_name="discrepancy_cache")
    op.drop_table("discrepancy_cache")
