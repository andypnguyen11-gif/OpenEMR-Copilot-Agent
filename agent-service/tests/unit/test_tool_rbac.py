"""Per-tool RBAC contract — both denial layers in one place.

The Tool ABC enforces authorization in two layers (see
:mod:`clinical_copilot.tools.base` docstring):

1. **JWT-side**, before any fetch. Covered exhaustively per concrete tool
   in :mod:`tests.unit.test_tools` — this file deliberately keeps a thin
   restatement so the contract test stands alone.
2. **FHIR-ACL-side**, when ``_run`` raises
   :class:`FhirAuthorizationDeniedError` after the JWT check passed. PR 7
   ships the contract; PR 8's FHIR-backed tools are the first concrete
   callers. Until then, this file uses a stub ``Tool`` subclass to drive
   the new branch end-to-end.

Both branches must produce the same audit/exception surface — the
orchestrator's abstention layer relies on having one shape to handle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

import pytest

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.tools.base import (
    FhirAuthorizationDeniedError,
    Tool,
    UnauthorizedToolCallError,
)
from clinical_copilot.tools.records import AnyRecord, ProblemRecord

AUDIT_SALT = "test-salt"
ALL_SCOPES = ["system/Condition.read"]


class _RecordingAuditWriter(AuditLogWriter):
    """In-memory drop-in for the real writer — see test_tools.py for the
    rationale behind subclassing rather than ducking the type.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StubFhirDeniedTool(Tool):
    """Tool whose ``_run`` always raises FhirAuthorizationDeniedError.

    Stands in for any future FHIR-backed tool whose upstream call comes
    back 401 / 403. The required_scope is satisfied by the test's claims
    so execution actually reaches ``_run``.
    """

    name: ClassVar[str] = "stub_fhir_denied"
    description: ClassVar[str] = "Stub tool that simulates a FHIR ACL denial."
    required_scope: ClassVar[str] = "system/Condition.read"
    record_kind: ClassVar[str] = "Condition"

    def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
        raise FhirAuthorizationDeniedError(
            f"upstream FHIR returned 403 for Condition?patient={patient_id}",
        )


class _StubHappyTool(Tool):
    """Tool whose ``_run`` returns one record — used only to confirm the
    happy path stays untouched by the new try/except in execute().
    """

    name: ClassVar[str] = "stub_happy"
    description: ClassVar[str] = "Stub tool that returns one record."
    required_scope: ClassVar[str] = "system/Condition.read"
    record_kind: ClassVar[str] = "Condition"

    def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
        return [
            ProblemRecord(
                source_id=f"Condition/{patient_id}-stub-1",
                code="E11.9",
                display="Type 2 diabetes",
                status="active",
            ),
        ]


def _claims(patient_id: str, *, scopes: list[str] | None = None) -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id=patient_id,
        scopes=scopes if scopes is not None else ALL_SCOPES,
        nonce="n-test",
        jti=f"jti-{patient_id}",
    )


def test_fhir_acl_denial_writes_unauthorized_audit_row_and_raises() -> None:
    """The new contract: ``_run`` raising FhirAuthorizationDeniedError
    must trip the same denial surface as a JWT-side miss.
    """

    audit = _RecordingAuditWriter()
    tool = _StubFhirDeniedTool(audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims("101")

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims, patient_id="101", request_id="req-fhir-denied")

    assert excinfo.value.tool_name == "stub_fhir_denied"
    assert excinfo.value.requested_patient_id == "101"
    # Cause chain preserved — the orchestrator's diagnostic logging walks
    # __cause__ to surface the upstream FHIR diagnostic in the trace
    # without leaking it to the user.
    assert isinstance(excinfo.value.__cause__, FhirAuthorizationDeniedError)

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "UNAUTHORIZED"
    assert event.resource_type == "stub_fhir_denied"
    assert event.user_id == "dr-patel"
    assert event.role == "physician"
    assert event.request_id == "req-fhir-denied"
    # Hash is over the *requested* patient_id (matches the JWT-side
    # denial path's invariant in test_tools.py).
    assert event.patient_id_hash == hash_patient_id("101", salt=AUDIT_SALT)


def test_fhir_acl_denial_path_is_fail_closed_on_audit_write_failure() -> None:
    """If the audit writer raises, the request must surface that — never
    UNAUTHORIZED without a logged row. The original FHIR exception
    information is lost in the cause chain (replaced by the audit error),
    which is intentional: the audit failure is the bigger signal.
    """

    class _BrokenAuditWriter(AuditLogWriter):
        def __init__(self) -> None:
            pass

        def write(self, event: AuditEvent) -> None:
            raise RuntimeError("postgres unreachable")

    tool = _StubFhirDeniedTool(audit=_BrokenAuditWriter(), audit_salt=AUDIT_SALT)
    claims = _claims("101")

    with pytest.raises(RuntimeError, match="postgres unreachable"):
        tool.execute(claims=claims, patient_id="101", request_id="req-broken")


def test_jwt_side_denial_still_short_circuits_before_run() -> None:
    """Restating the JWT-side contract here so this file documents both
    layers. A patient-id mismatch must deny *before* ``_run`` is invoked
    — exercised by using the FHIR-denied stub as a tripwire: if the base
    class let execution reach ``_run``, we'd see the FHIR error instead
    of the JWT-side denial.
    """

    audit = _RecordingAuditWriter()
    tool = _StubFhirDeniedTool(audit=audit, audit_salt=AUDIT_SALT)
    # Session is scoped to 101; model attempts the out-of-panel sentinel.
    claims = _claims("101")

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims, patient_id="999", request_id="req-jwt-deny")

    # No __cause__ — the JWT-side denial doesn't chain anything.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.requested_patient_id == "999"
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id("999", salt=AUDIT_SALT)


def test_missing_required_scope_denies_even_when_patient_matches() -> None:
    """Restated JWT-side contract: scope miss + patient match still denies."""

    audit = _RecordingAuditWriter()
    tool = _StubFhirDeniedTool(audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims("101", scopes=["system/Observation.read"])

    with pytest.raises(UnauthorizedToolCallError):
        tool.execute(claims=claims, patient_id="101", request_id="req-noscope")

    assert len(audit.events) == 1
    assert audit.events[0].action == "UNAUTHORIZED"


def test_happy_path_unaffected_by_fhir_denial_try_except() -> None:
    """A tool whose ``_run`` returns records normally must still produce
    a clean ToolResult — confirms the FHIR-denial try/except didn't
    accidentally swallow a non-denial path. A SUCCESS audit row is
    expected (ARCHITECTURE §8.3: every PHI access is logged).
    """

    audit = _RecordingAuditWriter()
    tool = _StubHappyTool(audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims("101")

    result = tool.execute(claims=claims, patient_id="101", request_id="req-happy")

    assert result.tool_name == "stub_happy"
    assert result.patient_id == "101"
    assert len(result.records) == 1
    assert result.records[0].source_id == "Condition/101-stub-1"
    assert len(audit.events) == 1
    assert audit.events[0].action == "SUCCESS"


def test_unrelated_run_exceptions_are_not_translated_to_unauthorized() -> None:
    """``_run`` raising a generic exception must propagate untouched —
    the FHIR-denial try/except is FhirAuthorizationDeniedError-only,
    not a catch-all that would mask real bugs as UNAUTHORIZED. A non-
    RBAC exception is also NOT a PHI access, so no SUCCESS row is
    written either: the audit table holds rows for resolved accesses,
    not for faulted ones.
    """

    class _StubBoomTool(Tool):
        name: ClassVar[str] = "stub_boom"
        description: ClassVar[str] = "Stub that raises a non-RBAC exception."
        required_scope: ClassVar[str] = "system/Condition.read"
        record_kind: ClassVar[str] = "Condition"

        def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
            raise RuntimeError("upstream parse error")

    audit = _RecordingAuditWriter()
    tool = _StubBoomTool(audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims("101")

    with pytest.raises(RuntimeError, match="upstream parse error"):
        tool.execute(claims=claims, patient_id="101", request_id="req-boom")

    # No audit row — this isn't a denial OR a successful access.
    assert audit.events == []
