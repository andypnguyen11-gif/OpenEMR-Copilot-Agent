"""Unit tests for :class:`FhirChartProvider`.

Coverage targets, in priority order:

* **Parallel chart load.** ``load_chart`` issues all six resource
  searches and projects each result into the matching record kind.
  Reuse of the per-tool projection helpers is the contract that keeps
  ``get_flags`` byte-identical regardless of which registry built the
  chart, so the assertions check both the shape and the projection
  outputs (not just counts).
* **ACL denial.** A 401 / 403 from any one resource search surfaces as
  :class:`FhirAuthorizationDeniedError` so the upstream
  :class:`GetFlagsTool` writes an UNAUTHORIZED audit row through the
  same path a JWT-side denial uses. Pinned here so a regression in the
  shared :data:`FHIR_ACL_DENIAL_STATUSES` constant lands on this test
  before it reaches integration.
* **Non-ACL fault.** A 500 propagates as the raw :class:`FhirError`;
  the orchestrator's ``TOOL_FAILURE`` branch handles it upstream — out
  of scope here.
* **Drop-on-missing.** Resources the per-tool projection drops (no
  code, no body, no date, etc.) do not appear in the chart. The chart
  loader is a thin reuse of those helpers, so this is a smoke check
  rather than re-pinning every projection's drop conditions.
"""

from __future__ import annotations

import pytest

from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    AllergyIntolerance,
    Attachment,
    CodeableConcept,
    Coding,
    Condition,
    DocumentReference,
    DocumentReferenceContent,
    Encounter,
    MedicationRequest,
    Observation,
    Period,
    Quantity,
)
from clinical_copilot.discrepancy.chart_provider import FhirChartProvider
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import FhirAuthorizationDeniedError

from ._fhir_tool_helpers import PATIENT_ID, StubFhirClient


def _condition() -> Condition:
    return Condition(
        id="cond-1",
        code=CodeableConcept(coding=[Coding(code="44054006", display="Type 2 diabetes")]),
        clinicalStatus=CodeableConcept(coding=[Coding(code="active", display=None)]),
        onsetDateTime="2019-04-12",
        onsetPeriod=None,
    )


def _medication() -> MedicationRequest:
    return MedicationRequest(
        id="med-1",
        status="active",
        medicationCodeableConcept=CodeableConcept(
            coding=[Coding(code="11111", display="Metformin 500mg")],
        ),
        medicationReference=None,
        dosageInstruction=[],
        authoredOn="2020-01-15",
    )


def _allergy() -> AllergyIntolerance:
    return AllergyIntolerance(
        id="allergy-1",
        code=CodeableConcept(coding=[Coding(code="penicillin", display="Penicillin")]),
        criticality="high",
        reaction=[],
    )


def _observation() -> Observation:
    return Observation(
        id="lab-1",
        code=CodeableConcept(coding=[Coding(code="2345-7", display="Glucose")]),
        valueQuantity=Quantity(value=120, unit="mg/dL"),
        effectiveDateTime="2024-09-10",
        referenceRange=[],
    )


def _encounter() -> Encounter:
    return Encounter(
        id="enc-1",
        type=[CodeableConcept(coding=[Coding(code="AMB", display="Ambulatory")])],
        period=Period(start="2024-09-10", end=None),
        reasonCode=[],
    )


def _document() -> DocumentReference:
    # "Visit summary" base64-encoded.
    body_b64 = "VmlzaXQgc3VtbWFyeQ=="
    return DocumentReference(
        id="doc-1",
        date="2024-09-10",
        author=[],
        content=[
            DocumentReferenceContent(
                attachment=Attachment(contentType="text/plain", data=body_b64, url=None),
            ),
        ],
    )


def test_load_chart_projects_all_six_resource_types(bridge: AsyncBridge) -> None:
    fhir = StubFhirClient(
        conditions=lambda *, patient_id: [_condition()],
        medications=lambda *, patient_id: [_medication()],
        allergies=lambda *, patient_id: [_allergy()],
        labs=lambda *, patient_id: [_observation()],
        encounters=lambda *, patient_id: [_encounter()],
        documents=lambda *, patient_id: [_document()],
    )
    provider = FhirChartProvider(fhir=fhir, bridge=bridge)

    chart = provider.load_chart(PATIENT_ID)

    # All six search methods invoked exactly once for this patient. The
    # gather() call doesn't expose order to assert on, so the contract
    # is: every kind was called for the right patient_id.
    kinds_called = sorted(kind for kind, pid in fhir.calls if pid == PATIENT_ID)
    assert kinds_called == [
        "allergies",
        "conditions",
        "documents",
        "encounters",
        "labs",
        "medications",
    ]

    assert chart.patient_id == PATIENT_ID
    assert [p.source_id for p in chart.problems] == ["Condition/cond-1"]
    assert [m.source_id for m in chart.medications] == ["MedicationRequest/med-1"]
    assert [a.source_id for a in chart.allergies] == ["AllergyIntolerance/allergy-1"]
    assert [lab.source_id for lab in chart.labs] == ["Observation/lab-1"]
    assert [v.source_id for v in chart.visits] == ["Encounter/enc-1"]
    assert [n.source_id for n in chart.notes] == ["DocumentReference/doc-1"]


def test_load_chart_returns_empty_chart_when_patient_has_no_records(
    bridge: AsyncBridge,
) -> None:
    # All handlers default to [] in StubFhirClient — exercises the
    # "in-panel patient with no records" branch without an error path.
    fhir = StubFhirClient()
    provider = FhirChartProvider(fhir=fhir, bridge=bridge)

    chart = provider.load_chart(PATIENT_ID)

    assert chart.problems == ()
    assert chart.medications == ()
    assert chart.allergies == ()
    assert chart.labs == ()
    assert chart.visits == ()
    assert chart.notes == ()


@pytest.mark.parametrize("status_code", [401, 403])
def test_acl_denial_on_any_resource_raises_authorization_denied(
    bridge: AsyncBridge,
    status_code: int,
) -> None:
    # Conditions deny; the other five would resolve normally if reached.
    # gather()'s fail-fast is what we're asserting on — one denial aborts
    # the chart load before partial data lands.
    fhir = StubFhirClient(
        conditions=FhirError("denied", status_code=status_code),
        medications=lambda *, patient_id: [_medication()],
        allergies=lambda *, patient_id: [_allergy()],
        labs=lambda *, patient_id: [_observation()],
        encounters=lambda *, patient_id: [_encounter()],
        documents=lambda *, patient_id: [_document()],
    )
    provider = FhirChartProvider(fhir=fhir, bridge=bridge)

    with pytest.raises(FhirAuthorizationDeniedError):
        provider.load_chart(PATIENT_ID)


def test_non_acl_fhir_error_propagates_unchanged(bridge: AsyncBridge) -> None:
    fhir = StubFhirClient(
        conditions=FhirError("upstream blew up", status_code=500),
    )
    provider = FhirChartProvider(fhir=fhir, bridge=bridge)

    with pytest.raises(FhirError) as excinfo:
        provider.load_chart(PATIENT_ID)
    assert excinfo.value.status_code == 500
    assert not isinstance(excinfo.value, FhirAuthorizationDeniedError)


def test_drop_on_missing_filters_unprojectable_resources(bridge: AsyncBridge) -> None:
    # Condition with no code → projection returns None → must not appear
    # in the chart. Smoke test for the "_drop_none reuses helpers" wiring;
    # per-projection drop semantics are pinned in the per-tool tests.
    bare = Condition(
        id="cond-2",
        code=None,
        clinicalStatus=None,
        onsetDateTime=None,
        onsetPeriod=None,
    )
    fhir = StubFhirClient(
        conditions=lambda *, patient_id: [_condition(), bare],
    )
    provider = FhirChartProvider(fhir=fhir, bridge=bridge)

    chart = provider.load_chart(PATIENT_ID)

    assert [p.source_id for p in chart.problems] == ["Condition/cond-1"]
