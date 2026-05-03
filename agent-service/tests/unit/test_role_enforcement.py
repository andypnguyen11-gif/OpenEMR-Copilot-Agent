"""Tests for Role enum, JWT-claim parsing, and per-role tool enforcement.

Three layers covered here:

1. **Enum contract** (``Role`` cases + ``from_claim``) — the wire format that
   round-trips with the PHP gateway's matching enum. Forward-compat: an
   unrecognised role string resolves to ``Role.UNKNOWN`` rather than raising,
   so a deploy that adds a PHP role case before the Python side ships fails
   closed (deny at tool layer) instead of 5xx-ing the verifier.

2. **JWT verifier** parses the ``role`` claim into a ``Role`` enum on
   ``ClinicianClaims``. Downstream code keys off the enum, never the raw
   string — the tool layer's ``allowed_roles`` set comparison would silently
   fail if any caller smuggled in a string.

3. **Tool RBAC** denies any claim whose role is not in the tool's
   ``allowed_roles`` ClassVar. Default allow-list is the three known clinical
   roles ``{PHYSICIAN, RESIDENT, SUPERVISOR}``; ``UNKNOWN`` is denied by
   omission. UNAUTHORIZED audit row is written before the raise (same
   surface as the patient-id and scope checks), so an attacker probing the
   role boundary leaves a trail.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

import pytest

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.tools.base import Tool, UnauthorizedToolCallError
from clinical_copilot.tools.records import AnyRecord, ProblemRecord

AUDIT_SALT = "test-salt"
ALL_SCOPES = ["system/Condition.read"]


class _RecordingAuditWriter(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StubHappyTool(Tool):
    """Default-allowed-roles tool that returns a single record on ``_run``."""

    name: ClassVar[str] = "stub_happy"
    description: ClassVar[str] = "Stub tool — used to drive role-allow checks."
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


class _SupervisorOnlyTool(Tool):
    """Custom subclass restricting access to SUPERVISOR.

    Stands in for the supervisor audit-log read endpoint that lands in a
    later slice. The contract this exercises is generic: any tool can
    narrow ``allowed_roles`` and the base class enforces it identically.
    """

    name: ClassVar[str] = "supervisor_only"
    description: ClassVar[str] = "Stub tool restricted to SUPERVISOR role."
    required_scope: ClassVar[str] = "system/Condition.read"
    record_kind: ClassVar[str] = "Condition"
    allowed_roles: ClassVar[frozenset[Role]] = frozenset({Role.SUPERVISOR})

    def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
        return []


def _claims(role: Role, *, patient_id: str = "101") -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=role,
        patient_id=patient_id,
        scopes=ALL_SCOPES,
        nonce="n-test",
        jti=f"jti-{role.value}-{patient_id}",
    )


# ---------------------------------------------------------------------------
# Enum contract
# ---------------------------------------------------------------------------


def test_enum_values_match_php_gateway_wire_format() -> None:
    # The string values are the JWT contract with the PHP gateway's matching
    # OpenEMR\Services\Copilot\Auth\Role enum. Any rename here breaks
    # round-trip until both sides ship together — pin the values explicitly.
    assert Role.UNKNOWN.value == "unknown"
    assert Role.PHYSICIAN.value == "physician"
    assert Role.RESIDENT.value == "resident"
    assert Role.SUPERVISOR.value == "supervisor"


def test_from_claim_parses_every_known_value() -> None:
    for role in Role:
        assert Role.from_claim(role.value) is role


def test_from_claim_returns_unknown_for_unrecognised_value() -> None:
    # Forward-compatibility: a future PHP enum case (e.g. "fellow") must
    # not crash the verifier. Resolves to UNKNOWN; the tool layer denies
    # UNKNOWN at the next boundary so the request still fails closed.
    assert Role.from_claim("fellow") is Role.UNKNOWN
    assert Role.from_claim("") is Role.UNKNOWN
    assert Role.from_claim("PHYSICIAN") is Role.UNKNOWN  # case-sensitive


# ---------------------------------------------------------------------------
# Tool-layer enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [Role.PHYSICIAN, Role.RESIDENT, Role.SUPERVISOR],
)
def test_default_allowed_roles_admits_each_clinical_role(role: Role) -> None:
    # Default allow-list covers the three MVP clinical roles — none of
    # them should be denied by the role check. The tool's required_scope
    # and patient_id match in this fixture, so any UnauthorizedToolCallError
    # here would specifically be the role gate firing.
    audit = _RecordingAuditWriter()
    tool = _StubHappyTool(audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(
        claims=_claims(role),
        patient_id="101",
        request_id=f"req-{role.value}",
    )

    assert result.tool_name == "stub_happy"
    assert audit.events == []


def test_unknown_role_denies_with_audit_row_and_no_run() -> None:
    # UNKNOWN is the deny-by-default sentinel — the resolver couldn't
    # classify the user, and granting any scope here would leak chart
    # data to a principal whose role we don't trust. Audit row first
    # (so the probe is logged), then raise.
    audit = _RecordingAuditWriter()
    tool = _StubHappyTool(audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(
            claims=_claims(Role.UNKNOWN),
            patient_id="101",
            request_id="req-unknown",
        )

    assert excinfo.value.tool_name == "stub_happy"
    assert excinfo.value.requested_patient_id == "101"

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "UNAUTHORIZED"
    assert event.role == "unknown"  # AuditEvent stores wire format
    assert event.user_id == "dr-patel"
    assert event.resource_type == "stub_happy"
    assert event.request_id == "req-unknown"
    assert event.patient_id_hash == hash_patient_id("101", salt=AUDIT_SALT)


def test_role_outside_custom_allowed_set_denies() -> None:
    # SupervisorOnlyTool restricts allowed_roles to {SUPERVISOR}.
    # PHYSICIAN and RESIDENT — even with the right scope and patient
    # match — must be denied at the role boundary.
    audit = _RecordingAuditWriter()
    tool = _SupervisorOnlyTool(audit=audit, audit_salt=AUDIT_SALT)

    for denied_role in (Role.PHYSICIAN, Role.RESIDENT):
        with pytest.raises(UnauthorizedToolCallError):
            tool.execute(
                claims=_claims(denied_role),
                patient_id="101",
                request_id=f"req-{denied_role.value}",
            )

    # One audit row per denial, role recorded as the wire string.
    assert len(audit.events) == 2
    recorded_roles = {event.role for event in audit.events}
    assert recorded_roles == {"physician", "resident"}
    assert all(event.action == "UNAUTHORIZED" for event in audit.events)


def test_role_in_custom_allowed_set_admits() -> None:
    audit = _RecordingAuditWriter()
    tool = _SupervisorOnlyTool(audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(
        claims=_claims(Role.SUPERVISOR),
        patient_id="101",
        request_id="req-sup",
    )

    assert result.tool_name == "supervisor_only"
    assert audit.events == []


def test_role_check_runs_before_run_method() -> None:
    # Mirrors the patient/scope-mismatch invariant in test_tool_rbac.py:
    # a role-mismatch must short-circuit before _run, so a tool whose
    # _run would raise (or do PHI work) cannot leak through a denied
    # role check. We assert this by giving the tool a _run that would
    # raise a distinctive exception — if that exception escapes, the
    # role gate ran too late.
    class _StubBoomTool(Tool):
        name: ClassVar[str] = "stub_boom_role"
        description: ClassVar[str] = "Stub whose _run would raise."
        required_scope: ClassVar[str] = "system/Condition.read"
        record_kind: ClassVar[str] = "Condition"

        def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
            raise AssertionError("_run must not be called when role denied")

    audit = _RecordingAuditWriter()
    tool = _StubBoomTool(audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError):
        tool.execute(
            claims=_claims(Role.UNKNOWN),
            patient_id="101",
            request_id="req-boom",
        )

    assert len(audit.events) == 1
    assert audit.events[0].action == "UNAUTHORIZED"
