"""Unit tests for ``GetVisitsFhirTool``.

Coverage in priority order:

* **Type fallback** — first ``Encounter.type`` entry with a display
  wins; an encounter with no displayable type lands as the literal
  ``"Encounter"`` so the row survives for the visit-date citation.
* **Drop-on-missing-period** — a visit with no ``period.start`` is
  dropped (no dateless visit can be cited reliably).
* **Chief complaint** — first ``reasonCode`` entry with a display.
* **ACL denial** — 401/403 → :class:`UnauthorizedToolCallError`.
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    CodeableConcept,
    Coding,
    Encounter,
    Period,
)
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.records import VisitRecord
from clinical_copilot.tools.visits import GetVisitsFhirTool

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)


def _encounter(
    *,
    eid: str,
    type_display: str | None = "Office Visit",
    period_start: str | None = "2026-03-14",
    reason_text: str | None = "Routine diabetes follow-up",
) -> Encounter:
    return Encounter(
        id=eid,
        status="finished",
        type=(
            [CodeableConcept(coding=[Coding(display=type_display)], text=type_display)]
            if type_display is not None
            else []
        ),
        period=Period(start=period_start, end=None) if period_start is not None else None,
        reasonCode=([CodeableConcept(text=reason_text)] if reason_text is not None else []),
    )


def test_projects_encounter_to_visit_record(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(encounters=lambda *, patient_id: [_encounter(eid="p101-enc-1")])
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert len(result.records) == 1
    record = expect_record(result.records[0], VisitRecord)
    assert record.source_id == "Encounter/p101-enc-1"
    assert record.encounter_type == "Office Visit"
    assert record.visited_on == "2026-03-14"
    assert record.chief_complaint == "Routine diabetes follow-up"


def test_falls_back_to_encounter_literal_when_type_missing(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    enc = _encounter(eid="p101-enc-2", type_display=None)
    fhir = StubFhirClient(encounters=lambda *, patient_id: [enc])
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], VisitRecord).encounter_type == "Encounter"


def test_drops_encounter_without_period_start(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = _encounter(eid="p101-enc-bad", period_start=None)
    keepable = _encounter(eid="p101-enc-1")
    fhir = StubFhirClient(encounters=lambda *, patient_id: [droppable, keepable])
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    assert len(result.records) == 1
    assert result.records[0].source_id == "Encounter/p101-enc-1"


def test_chief_complaint_is_none_when_no_reason_code(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    enc = _encounter(eid="p101-enc-1", reason_text=None)
    fhir = StubFhirClient(encounters=lambda *, patient_id: [enc])
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    assert expect_record(result.records[0], VisitRecord).chief_complaint is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_writes_audit_and_raises(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        encounters=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_visits"
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)


def test_empty_bundle_returns_empty_records(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(encounters=lambda *, patient_id: [])
    tool = GetVisitsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-empty")

    assert result.records == []
