"""Read companion to :class:`AuditLogWriter`.

Powers the supervisor audit-log endpoint (PR 18 / ARCHITECTURE §8.3): a
SUPERVISOR caller fetches the rows they're allowed to review for a given
resident user id. The reader is intentionally small — there is no cross-
user search, no aggregation, and no PHI un-hashing path here. The route
gates *who* may call; this module just turns "(user_id, limit, offset)"
into a typed list of rows.

The audit table only stores hashed patient IDs (PR 2 contract — see
:mod:`clinical_copilot.audit.log`), so :class:`AuditLogEntry` is safe to
return over the wire as-is. Adding a join to a patients table here would
break that property and is explicitly out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from clinical_copilot.db.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker


# Cap any single supervisor request. Larger pages would let one
# misconfigured client exhaust the read connection while others wait;
# pagination via ``offset`` is the supported way to walk a long history.
MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class AuditLogEntry:
    """Wire-shape projection of one ``audit_log`` row.

    Frozen + slots so the route serializer can hand these straight into
    Pydantic without an intermediate dict — keeping the projection
    explicit (rather than dumping the ORM row) is what guarantees a
    future column add doesn't silently leak through the read endpoint.
    """

    ts: datetime
    user_id: str
    role: str
    patient_id_hash: str
    resource_type: str
    action: str
    request_id: str


class AuditLogReader:
    """Bounded, user-scoped reader over the ``audit_log`` table.

    Constructed with the same sessionmaker the writer uses so reads see
    rows the writer just committed (no replica-lag surprise during
    integration tests). Each call opens a short-lived session and
    closes it on the way out — there is no connection caching here.
    """

    def __init__(self, session_factory: sessionmaker[SqlSession]) -> None:
        self._session_factory = session_factory

    def list_for_user(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLogEntry]:
        """Return audit rows for ``user_id`` ordered newest-first.

        ``limit`` is clamped to :data:`MAX_PAGE_SIZE`; the route layer
        validates the request-side bound, but defending here too means
        an internal caller can't accidentally over-page.
        """

        if not user_id:
            raise ValueError("user_id must be non-empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        bounded_limit = min(limit, MAX_PAGE_SIZE)

        stmt = (
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .limit(bounded_limit)
            .offset(offset)
        )
        with self._session_factory() as session:
            rows = session.scalars(stmt).all()

        return [
            AuditLogEntry(
                ts=row.ts,
                user_id=row.user_id,
                role=row.role,
                patient_id_hash=row.patient_id_hash,
                resource_type=row.resource_type,
                action=row.action,
                request_id=row.request_id,
            )
            for row in rows
        ]
