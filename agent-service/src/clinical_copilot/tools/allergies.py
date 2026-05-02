"""``get_allergies`` — FHIR ``AllergyIntolerance`` reads.

Allergies gate every medication-safety claim the agent makes; the
verification middleware (PR 11) requires a citation against an
:class:`AllergyRecord` whenever prose mentions an allergy. The tool
projects one record per FHIR resource.

Notes on the projection:

* ``substance`` is the resource's ``code`` rendered through
  :meth:`CodeableConcept.preferred_display`. An allergy with no
  identifiable substance can't be cited safely — it would render as a
  blank in the UI — so those entries are dropped.
* ``reaction`` flattens the FHIR list of reactions into a single string:
  the first manifestation display of the first reaction. PRD §3 only
  surfaces the resource-level summary today; PR 13's rules engine will
  revisit this when manifestation-level matching matters.
* ``severity`` prefers resource-level ``criticality`` (low / high /
  unable-to-assess) because OpenEMR populates it more reliably than
  per-reaction severity. The per-reaction severity is the fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.data.models import AllergyIntolerance
from clinical_copilot.tools.fhir_base import FhirBackedTool, reference_id
from clinical_copilot.tools.records import AllergyRecord


class GetAllergiesFhirTool(FhirBackedTool):
    name: ClassVar[str] = "get_allergies"
    description: ClassVar[str] = (
        "Return the patient's allergies and intolerances "
        "(AllergyIntolerance resources). Required before any "
        "medication-safety claim."
    )
    required_scope: ClassVar[str] = "system/AllergyIntolerance.read"
    record_kind: ClassVar[str] = "AllergyIntolerance"

    async def _fetch(self, *, patient_id: str) -> Sequence[AllergyRecord]:
        allergies = await self._fhir.search_allergies(patient_id=patient_id)
        records: list[AllergyRecord] = []
        for allergy in allergies:
            record = _project(allergy)
            if record is not None:
                records.append(record)
        return records


def _project(allergy: AllergyIntolerance) -> AllergyRecord | None:
    substance = allergy.code.preferred_display() if allergy.code else None
    if not substance:
        return None
    return AllergyRecord(
        source_id=reference_id("AllergyIntolerance", allergy.id),
        substance=substance,
        reaction=_reaction(allergy),
        severity=_severity(allergy),
    )


def _reaction(allergy: AllergyIntolerance) -> str | None:
    for reaction in allergy.reaction:
        for manifestation in reaction.manifestation:
            display = manifestation.preferred_display()
            if display:
                return display
        if reaction.description:
            return reaction.description
    return None


def _severity(allergy: AllergyIntolerance) -> str | None:
    if allergy.criticality:
        return allergy.criticality
    for reaction in allergy.reaction:
        if reaction.severity:
            return reaction.severity
    return None
