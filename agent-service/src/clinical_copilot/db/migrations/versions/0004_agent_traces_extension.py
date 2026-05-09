"""agent_traces_extension: retrieval_hits + extraction_confidence

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-09

Two nullable columns hung off ``agent_traces`` so a single trace row can
describe whichever shape of work the request did:

* ``retrieval_hits INTEGER NULL`` — number of chunks the evidence
  retriever surfaced this turn. ``NULL`` on chart-only / fast-lane turns
  that never invoked retrieval; ``0`` is a real value (retriever ran but
  returned nothing).
* ``extraction_confidence REAL NULL`` — mean per-field confidence from
  the document extractor. ``NULL`` on retrieval-only turns; populated on
  the document-ingest entry point that runs the multimodal extractor.

NULL semantics are independent per column. A slow-lane retrieval-only
turn writes ``retrieval_hits=N, extraction_confidence=NULL``; a
document-ingest writes ``retrieval_hits=NULL, extraction_confidence=f``;
nothing today writes both. ``REAL`` (not ``Numeric``) keeps the value
portable across SQLite (dev) and Postgres (prod) without forcing a
``DECIMAL`` precision on a value that's already a bounded float.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_traces",
        sa.Column("retrieval_hits", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agent_traces",
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_traces", "extraction_confidence")
    op.drop_column("agent_traces", "retrieval_hits")
