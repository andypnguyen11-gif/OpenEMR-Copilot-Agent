"""Audit-log writer and patient-ID hashing.

ARCHITECTURE §7 / §8: every PHI access produces an ``audit_log`` row, and
that row is committed *before* the request that triggered the access is
allowed to succeed. If the DB is unreachable, the writer raises — the caller
must propagate the failure to the user (translating to a 500), never swallow.

Patient IDs are stored only as HMAC-SHA256 hashes. The salt is per-environment
(prod and dev have different salts; rotating the salt invalidates historical
joins, which is intentional). Use a constant-time HMAC rather than a plain
SHA-256 to prevent rainbow-table attacks against the small space of patient
IDs in any one OpenEMR instance.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from clinical_copilot.db.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker

    from clinical_copilot.audit.models import AuditEvent


def hash_patient_id(patient_id: str, *, salt: str) -> str:
    """Return ``HMAC-SHA256(salt, patient_id)`` as a 64-char hex string.

    The salt is treated as opaque bytes (UTF-8-encoded). Empty patient IDs
    are not allowed — pass an actual identifier.
    """

    if not patient_id:
        raise ValueError("patient_id must be non-empty")
    digest = hmac.new(salt.encode("utf-8"), patient_id.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


class AuditLogWriteError(RuntimeError):
    """Raised when an audit-log row cannot be committed.

    Callers must surface this to the request as a 5xx — the request is not
    allowed to succeed without a logged row. The original DB error is
    available via :attr:`__cause__`; the message here is intentionally
    generic so it's safe to log.
    """


class AuditLogWriter:
    """Fail-closed audit-log writer.

    One instance per app, constructed at startup with a sessionmaker. Each
    :meth:`write` call opens a fresh session, commits the row, and closes
    the session — short-lived sessions keep audit writes independent of any
    longer-running unit of work.
    """

    def __init__(self, session_factory: sessionmaker[SqlSession]) -> None:
        self._session_factory = session_factory

    def write(self, event: AuditEvent) -> None:
        """Persist ``event`` synchronously. Raises :class:`AuditLogWriteError`
        on any DB-side failure; the caller must not catch and continue.
        """

        row = AuditLog(
            user_id=event.user_id,
            role=event.role,
            patient_id_hash=event.patient_id_hash,
            resource_type=event.resource_type,
            action=event.action,
            request_id=event.request_id,
        )
        session = self._session_factory()
        try:
            session.add(row)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise AuditLogWriteError("failed to write audit-log row") from exc
        finally:
            session.close()
