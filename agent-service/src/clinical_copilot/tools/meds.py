"""``get_meds`` — FHIR ``MedicationRequest`` reads.

OpenEMR uses ``medicationCodeableConcept`` for inline drug names and
``medicationReference`` for catalog-linked entries. The projection
prefers the inline display because reference resolution would require a
second FHIR fetch per medication; PR 13's discrepancy engine doesn't
need the catalog code today.

Notes on the projection:

* ``name`` is required on :class:`MedicationRecord`; entries with
  neither inline display nor reference display are dropped — those
  would render as empty strings to the clinician.
* ``dose`` joins every populated ``dosageInstruction[].text`` so the
  rare multi-line frequency (e.g. "10 mg AM, 5 mg PM") survives the
  projection. ``None`` when no instruction text is set.
* ``status`` falls back to ``"unknown"`` so the record-level invariant
  (every :class:`MedicationRecord` has a status) holds even for
  poorly-coded historical prescriptions.
* PR 8 tracks only :class:`MedicationRequest`. ``MedicationStatement``
  (the patient-reported med list) is a separate FHIR resource OpenEMR
  doesn't populate today; PR 13 revisits whether to merge it in once
  the rules engine cares about reconciliation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.data.models import MedicationRequest
from clinical_copilot.tools.fhir_base import FhirBackedTool, reference_id
from clinical_copilot.tools.records import MedicationRecord

_DEFAULT_STATUS = "unknown"


class GetMedsFhirTool(FhirBackedTool):
    name: ClassVar[str] = "get_meds"
    description: ClassVar[str] = (
        "Return the patient's active and recent medications "
        "(MedicationRequest resources). Use for any med-list question "
        "or to anchor citations about prescriptions, doses, or status."
    )
    required_scope: ClassVar[str] = "system/MedicationRequest.read"
    record_kind: ClassVar[str] = "MedicationRequest"

    async def _fetch(self, *, patient_id: str) -> Sequence[MedicationRecord]:
        requests = await self._fhir.search_medications(patient_id=patient_id)
        records: list[MedicationRecord] = []
        for request in requests:
            record = project_medication_request_to_record(request)
            if record is not None:
                records.append(record)
        return records


def project_medication_request_to_record(request: MedicationRequest) -> MedicationRecord | None:
    name = _drug_name(request)
    if not name:
        return None
    return MedicationRecord(
        source_id=reference_id("MedicationRequest", request.id),
        name=name,
        dose=_dose(request),
        status=request.status or _DEFAULT_STATUS,
        started_on=request.authored_on,
    )


def _drug_name(request: MedicationRequest) -> str | None:
    if request.medication_codeable_concept:
        display = request.medication_codeable_concept.preferred_display()
        if display:
            return display
    if request.medication_reference and request.medication_reference.display:
        return request.medication_reference.display
    return None


def _dose(request: MedicationRequest) -> str | None:
    parts = [d.text for d in request.dosage_instruction if d.text]
    if not parts:
        return None
    # Multi-line dose text joins on " | " so it stays one record-level
    # field; the model gets the full instruction surface without a
    # nested list it would have to re-stringify.
    return " | ".join(parts)
