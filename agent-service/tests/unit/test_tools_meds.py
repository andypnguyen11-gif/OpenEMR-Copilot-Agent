"""Unit tests for ``GetMedsFhirTool``.

Coverage in priority order:

* **Inline-display path** — ``medicationCodeableConcept.text`` projects
  to ``name`` and ``dosageInstruction[0].text`` projects to ``dose``.
* **Reference-display fallback** — when only ``medicationReference``
  carries a display, that value is used (no second FHIR fetch).
* **Multi-line dosage** — multiple ``dosageInstruction`` entries join
  on ``" | "`` so the model gets the full instruction surface as a
  single record field.
* **ACL denial** — 401/403 from FHIR raises
  :class:`UnauthorizedToolCallError` + audit row.
* **Drop-on-empty-name** — a MedicationRequest with neither inline nor
  reference display is dropped (no empty-string drug name leaks).
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    CodeableConcept,
    Coding,
    Dosage,
    MedicationRequest,
    Reference,
)
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.meds import GetMedsFhirTool
from clinical_copilot.tools.records import MedicationRecord

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)


def _med(
    *,
    mid: str,
    inline_name: str | None = "Metformin 1000 mg tablet",
    reference_display: str | None = None,
    dose_lines: tuple[str, ...] = ("1 tablet PO BID",),
    status: str | None = "active",
    authored_on: str | None = "2019-04-20",
) -> MedicationRequest:
    return MedicationRequest(
        id=mid,
        status=status,
        medicationCodeableConcept=(
            CodeableConcept(coding=[Coding(display=inline_name)], text=inline_name)
            if inline_name is not None
            else None
        ),
        medicationReference=(
            Reference(reference=None, display=reference_display)
            if reference_display is not None
            else None
        ),
        authoredOn=authored_on,
        dosageInstruction=[Dosage(text=line) for line in dose_lines],
    )


def test_projects_inline_codeable_concept_to_name(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(medications=lambda *, patient_id: [_med(mid="p101-med-1")])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert len(result.records) == 1
    record = expect_record(result.records[0], MedicationRecord)
    assert record.source_id == "MedicationRequest/p101-med-1"
    assert record.name == "Metformin 1000 mg tablet"
    assert record.dose == "1 tablet PO BID"
    assert record.status == "active"
    assert record.started_on == "2019-04-20"


def test_falls_back_to_medication_reference_display(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    med = _med(mid="p101-med-2", inline_name=None, reference_display="Lisinopril 10 mg tablet")
    fhir = StubFhirClient(medications=lambda *, patient_id: [med])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], MedicationRecord).name == "Lisinopril 10 mg tablet"


def test_joins_multi_line_dosage_with_pipe(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    med = _med(mid="p101-med-3", dose_lines=("10 mg AM", "5 mg PM"))
    fhir = StubFhirClient(medications=lambda *, patient_id: [med])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    assert expect_record(result.records[0], MedicationRecord).dose == "10 mg AM | 5 mg PM"


def test_dose_is_none_when_no_instructions(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    med = _med(mid="p101-med-4", dose_lines=())
    fhir = StubFhirClient(medications=lambda *, patient_id: [med])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    assert expect_record(result.records[0], MedicationRecord).dose is None


def test_status_defaults_to_unknown_when_missing(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    med = _med(mid="p101-med-5", status=None)
    fhir = StubFhirClient(medications=lambda *, patient_id: [med])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-5")

    assert expect_record(result.records[0], MedicationRecord).status == "unknown"


def test_drops_request_with_no_resolvable_name(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = MedicationRequest(
        id="p101-med-bad",
        status="active",
        medicationCodeableConcept=None,
        medicationReference=None,
        authoredOn=None,
        dosageInstruction=[],
    )
    keepable = _med(mid="p101-med-1")
    fhir = StubFhirClient(medications=lambda *, patient_id: [droppable, keepable])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-6")

    assert len(result.records) == 1
    assert result.records[0].source_id == "MedicationRequest/p101-med-1"


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_writes_audit_and_raises(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        medications=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_meds"
    assert len(audit.events) == 1
    assert audit.events[0].action == "UNAUTHORIZED"
    assert audit.events[0].patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)


def test_empty_bundle_returns_empty_records(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(medications=lambda *, patient_id: [])
    tool = GetMedsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-empty")

    assert result.records == []
