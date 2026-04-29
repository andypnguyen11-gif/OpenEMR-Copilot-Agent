"""Typed value objects for audit-log entries.

The writer accepts an :class:`AuditEvent` rather than a raw ORM instance so
callers can't accidentally bypass field validation (e.g. by handing in a
patient ID instead of a hash). The conversion to an ORM row happens inside
the writer, where it's the only path to the table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One PHI-access event, ready to commit.

    ``patient_id_hash`` must already be hashed by
    :func:`clinical_copilot.audit.log.hash_patient_id` before construction;
    the writer does not re-hash. This keeps the hashing call site visible at
    the point where a raw patient ID is in scope.
    """

    user_id: str
    role: str
    patient_id_hash: str
    resource_type: str
    action: str
    request_id: str
