"""Unit tests for ``GetProblemsFhirTool``.

Coverage targets, in priority order:

* **Projection.** A FHIR ``Condition`` with a coded ``code`` and a
  populated ``clinicalStatus`` lands as a :class:`ProblemRecord` with
  the coded ``source_id`` shape (``Condition/<id>``) the verification
  middleware joins on.
* **ACL denial.** A 403 from the FHIR server raises
  :class:`UnauthorizedToolCallError` and writes one UNAUTHORIZED audit
  row — same surface as the JWT-side denial path. Pinned here as well
  as in ``test_tool_rbac.py`` so a regression in the
  ``FhirError`` → ``FhirAuthorizationDeniedError`` translation in
  ``fhir_base.py`` shows up at this layer.
* **Non-ACL fault.** A 500 from the FHIR server propagates as
  :class:`FhirError` (no audit row); the orchestrator translates it to
  ``TOOL_FAILURE`` upstream — out of scope here.
* **Drop-on-missing.** Conditions with neither a coded nor free-text
  display are dropped so the model never gets an empty-display row to
  cite.
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import CodeableConcept, Coding, Condition, Period
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.problems import GetProblemsFhirTool
from clinical_copilot.tools.records import ProblemRecord

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)


def _condition(
    *,
    cid: str,
    code: str = "44054006",
    display: str = "Type 2 diabetes mellitus",
    onset_date_time: str | None = "2019-04-12",
    onset_period: Period | None = None,
    status_code: str | None = "active",
    status_display: str | None = None,
) -> Condition:
    return Condition(
        id=cid,
        code=CodeableConcept(coding=[Coding(code=code, display=display)]),
        clinicalStatus=(
            CodeableConcept(coding=[Coding(code=status_code, display=status_display)])
            if status_code is not None
            else None
        ),
        onsetDateTime=onset_date_time,
        onsetPeriod=onset_period,
    )


def test_projects_condition_to_problem_record(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        conditions=lambda *, patient_id: [_condition(cid="p101-cond-1")],
    )
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert result.tool_name == "get_problems"
    assert result.patient_id == PATIENT_ID
    assert len(result.records) == 1
    record = expect_record(result.records[0], ProblemRecord)
    assert record.source_id == "Condition/p101-cond-1"
    assert record.code == "44054006"
    assert record.display == "Type 2 diabetes mellitus"
    assert record.onset_date == "2019-04-12"
    assert record.status == "active"
    assert audit.events == []


def test_falls_back_to_onset_period_start_when_datetime_missing(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    cond = _condition(
        cid="p101-cond-2",
        onset_date_time=None,
        onset_period=Period(start="2018-11-03", end=None),
    )
    fhir = StubFhirClient(conditions=lambda *, patient_id: [cond])
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], ProblemRecord).onset_date == "2018-11-03"


def test_drops_condition_with_no_code_or_display(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    keepable = _condition(cid="p101-cond-1")
    droppable = Condition(
        id="p101-cond-bad",
        code=CodeableConcept(coding=[Coding(code=None, display=None)]),
        clinicalStatus=None,
        onsetDateTime=None,
        onsetPeriod=None,
    )
    fhir = StubFhirClient(conditions=lambda *, patient_id: [droppable, keepable])
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    assert len(result.records) == 1
    assert result.records[0].source_id == "Condition/p101-cond-1"


def test_status_falls_back_to_unknown_when_clinical_status_missing(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    cond = _condition(cid="p101-cond-1", status_code=None)
    fhir = StubFhirClient(conditions=lambda *, patient_id: [cond])
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    assert expect_record(result.records[0], ProblemRecord).status == "unknown"


def test_empty_bundle_returns_empty_records(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(conditions=lambda *, patient_id: [])
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-5")

    assert result.records == []
    assert audit.events == []


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_raises_unauthorized_and_writes_audit(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        conditions=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_problems"
    assert excinfo.value.requested_patient_id == PATIENT_ID
    # Cause chain preserved so the orchestrator's logger can surface
    # the upstream diagnostic without leaking it to the user.
    assert isinstance(excinfo.value.__cause__, Exception)
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "UNAUTHORIZED"
    assert event.resource_type == "get_problems"
    assert event.patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)


def test_non_acl_fhir_error_propagates_without_audit(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        conditions=FhirError("FHIR server error: status=502", status_code=502),
    )
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(FhirError):
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-fault")

    assert audit.events == []


def test_jwt_side_denial_short_circuits_before_fhir_call(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    """The Tool ABC must enforce JWT-side RBAC before any FHIR call.
    Pin it here so a refactor that reorders the base class doesn't
    accidentally let an out-of-panel patient_id reach the FHIR client.
    """

    fhir = StubFhirClient(conditions=lambda *, patient_id: [_condition(cid="p101-cond-1")])
    tool = GetProblemsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)
    # Session bound to PATIENT_ID; model attempts the out-of-panel sentinel.
    claims = claims_for(PATIENT_ID)

    with pytest.raises(UnauthorizedToolCallError):
        tool.execute(claims=claims, patient_id="999", request_id="req-jwt-deny")

    assert fhir.calls == []
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id("999", salt=AUDIT_SALT)
