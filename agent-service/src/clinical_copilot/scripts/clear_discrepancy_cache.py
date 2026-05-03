"""Wipe the durable tier of :class:`DiscrepancyCache`.

Two-tier reminder: this only deletes rows from the ``discrepancy_cache``
table. The agent process keeps a parallel in-memory cache (see
``DiscrepancyCache._memory``) that survives a DB-only delete until the
30-minute TTL expires or the process restarts. After running this you
must either:

* restart the agent-service (drops the in-memory tier with the process), or
* call ``POST /api/agent/internal/invalidate/{patient_id}`` per affected
  pid (requires ``COPILOT_INTERNAL_TOKEN``) — the route invalidates both
  tiers atomically.

Reads ``DATABASE_URL`` via :class:`Settings` so SQLite-local and
Postgres-Railway both work without hardcoded paths. Invoke via::

    uv run python -m clinical_copilot.scripts.clear_discrepancy_cache
"""

from __future__ import annotations

import sys

from sqlalchemy import CursorResult, delete

from clinical_copilot.config import get_settings
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.db.models import DiscrepancyCacheRow


def main() -> int:
    settings = get_settings()
    engine = create_engine_from_url(settings.database_url)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        # ``Session.execute`` is typed as ``Result[Any]``, but DML
        # statements always produce a :class:`CursorResult` at runtime —
        # only that subclass exposes ``rowcount``. Narrowing here instead
        # of a ``# type: ignore`` keeps the type promise explicit.
        result = session.execute(delete(DiscrepancyCacheRow))
        session.commit()
        if not isinstance(result, CursorResult):
            raise RuntimeError("delete() did not return a CursorResult")
        deleted = result.rowcount or 0

    print(f"Deleted {deleted} row(s) from discrepancy_cache ({settings.database_url}).")
    print(
        "In-memory tier still hot — restart the agent-service or call "
        "POST /api/agent/internal/invalidate/{patient_id} for affected pids.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
