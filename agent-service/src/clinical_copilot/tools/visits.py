"""``get_visits`` — FHIR ``Encounter`` reads.

The tool returns recent visits as :class:`VisitRecord` rows. PRD §3
uses these to answer "when was the last visit" / "what was the chief
complaint" so the projection optimises for those two questions.

Notes on the projection:

* ``encounter_type`` walks ``Encounter.type`` (a list per FHIR R4) and
  picks the first entry with a display string. An encounter without
  any displayable type is rendered as ``"Encounter"`` — a coarse
  fallback that's better than dropping the row, because the visit date
  alone is often the citation target.
* ``visited_on`` requires ``Encounter.period.start``. A visit with no
  start date can't anchor a "when did this happen" claim — those rows
  are dropped (matching the docstring on
  :class:`clinical_copilot.data.models.Encounter`).
* ``chief_complaint`` projects the first ``reasonCode`` with a display.
  ``reasonReference`` (Condition reference) is intentionally not
  followed: a second FHIR fetch per encounter would inflate latency
  and the rules engine doesn't yet need it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.data.models import Encounter
from clinical_copilot.tools.fhir_base import FhirBackedTool, reference_id
from clinical_copilot.tools.records import VisitRecord

_DEFAULT_ENCOUNTER_TYPE = "Encounter"


class GetVisitsFhirTool(FhirBackedTool):
    name: ClassVar[str] = "get_visits"
    description: ClassVar[str] = (
        "Return the patient's recent encounters (Encounter resources). "
        "Use to answer 'when was the last visit' or 'what was the "
        "presenting complaint?'"
    )
    required_scope: ClassVar[str] = "system/Encounter.read"
    record_kind: ClassVar[str] = "Encounter"

    async def _fetch(self, *, patient_id: str) -> Sequence[VisitRecord]:
        encounters = await self._fhir.search_encounters(patient_id=patient_id)
        records: list[VisitRecord] = []
        for encounter in encounters:
            record = _project(encounter)
            if record is not None:
                records.append(record)
        return records


def _project(encounter: Encounter) -> VisitRecord | None:
    if encounter.period is None or not encounter.period.start:
        return None
    return VisitRecord(
        source_id=reference_id("Encounter", encounter.id),
        encounter_type=_encounter_type(encounter),
        visited_on=encounter.period.start,
        chief_complaint=_chief_complaint(encounter),
    )


def _encounter_type(encounter: Encounter) -> str:
    for type_concept in encounter.type:
        display = type_concept.preferred_display()
        if display:
            return display
    return _DEFAULT_ENCOUNTER_TYPE


def _chief_complaint(encounter: Encounter) -> str | None:
    for reason in encounter.reason_code:
        display = reason.preferred_display()
        if display:
            return display
    return None
