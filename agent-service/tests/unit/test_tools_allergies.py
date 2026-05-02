"""Unit tests for ``GetAllergiesFhirTool``.

Coverage in priority order:

* **Resource-level criticality wins over per-reaction severity** —
  OpenEMR populates ``criticality`` more reliably than per-reaction
  severity, and the projection inverts that order only when
  ``criticality`` is absent.
* **Reaction display fallback** — manifestation display, then
  ``description``; a reaction with neither contributes nothing rather
  than an empty string.
* **Drop on missing substance** — an :class:`AllergyIntolerance` with
  no display under ``code`` is dropped: there's nothing meaningful to
  cite.
* **ACL denial** — 401/403 → :class:`UnauthorizedToolCallError` +
  audit row.
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    AllergyIntolerance,
    AllergyIntoleranceReaction,
    CodeableConcept,
    Coding,
)
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.allergies import GetAllergiesFhirTool
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.records import AllergyRecord

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)


def _allergy(
    *,
    aid: str,
    substance_text: str | None = "Penicillin",
    criticality: str | None = "high",
    reactions: tuple[AllergyIntoleranceReaction, ...] = (),
) -> AllergyIntolerance:
    return AllergyIntolerance(
        id=aid,
        code=(
            CodeableConcept(coding=[Coding(display=substance_text)], text=substance_text)
            if substance_text is not None
            else None
        ),
        criticality=criticality,
        clinicalStatus=None,
        reaction=list(reactions),
    )


def test_projects_resource_level_criticality_to_severity(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    allergy = _allergy(
        aid="p101-allergy-1",
        criticality="high",
        reactions=(
            AllergyIntoleranceReaction(
                manifestation=[CodeableConcept(text="Anaphylaxis")],
                severity="severe",
                description=None,
            ),
        ),
    )
    fhir = StubFhirClient(allergies=lambda *, patient_id: [allergy])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert len(result.records) == 1
    record = expect_record(result.records[0], AllergyRecord)
    assert record.source_id == "AllergyIntolerance/p101-allergy-1"
    assert record.substance == "Penicillin"
    assert record.reaction == "Anaphylaxis"
    # Resource-level criticality, not per-reaction severity.
    assert record.severity == "high"


def test_falls_back_to_reaction_severity_when_no_criticality(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    allergy = _allergy(
        aid="p101-allergy-2",
        criticality=None,
        reactions=(
            AllergyIntoleranceReaction(
                manifestation=[CodeableConcept(text="Hives")],
                severity="moderate",
                description=None,
            ),
        ),
    )
    fhir = StubFhirClient(allergies=lambda *, patient_id: [allergy])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], AllergyRecord).severity == "moderate"


def test_reaction_falls_back_to_description_when_no_manifestation(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    allergy = _allergy(
        aid="p101-allergy-3",
        reactions=(
            AllergyIntoleranceReaction(
                manifestation=[],
                severity=None,
                description="Reported swelling without coded manifestation",
            ),
        ),
    )
    fhir = StubFhirClient(allergies=lambda *, patient_id: [allergy])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    assert (
        expect_record(result.records[0], AllergyRecord).reaction
        == "Reported swelling without coded manifestation"
    )


def test_drops_allergy_with_no_substance_display(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = _allergy(aid="p101-allergy-bad", substance_text=None, criticality=None)
    keepable = _allergy(aid="p101-allergy-1")
    fhir = StubFhirClient(allergies=lambda *, patient_id: [droppable, keepable])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    assert len(result.records) == 1
    assert result.records[0].source_id == "AllergyIntolerance/p101-allergy-1"


def test_reaction_is_none_when_neither_manifestation_nor_description(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    allergy = _allergy(
        aid="p101-allergy-5",
        reactions=(AllergyIntoleranceReaction(manifestation=[], severity=None, description=None),),
    )
    fhir = StubFhirClient(allergies=lambda *, patient_id: [allergy])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-5")

    assert expect_record(result.records[0], AllergyRecord).reaction is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_writes_audit_and_raises(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        allergies=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_allergies"
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)


def test_empty_bundle_returns_empty_records(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(allergies=lambda *, patient_id: [])
    tool = GetAllergiesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-empty")

    assert result.records == []
