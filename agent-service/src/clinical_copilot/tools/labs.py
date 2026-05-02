"""``get_labs`` — FHIR ``Observation`` reads, lab category only.

The lab-category filter is applied at the FHIR client layer
(``category=laboratory``) so vitals and social-history observations
never enter the lab tool's surface.

Notes on the projection:

* ``value`` is required on :class:`LabRecord`; the projection collapses
  the FHIR ``value[x]`` choice type to a single string. ``valueQuantity``
  wins because labs are numeric in the common case; ``valueString``
  covers free-text results (e.g. "Negative"); ``valueCodeableConcept``
  covers coded qualitative results. An Observation with none of the
  three is dropped — there's nothing to cite.
* ``observed_on`` is required and falls back from
  ``effectiveDateTime`` → ``None`` only when the FHIR record is
  malformed; in that case the row is dropped (a dateless lab can't be
  trended or cited).
* ``unit`` is only set when the value came from a Quantity. A free-text
  or coded result has no unit by definition.
* ``reference_range`` joins ``low-high`` from the first range entry
  with a populated bound. ``text`` wins when the FHIR resource carries
  a literal range string (e.g. "<5.7"). The Pydantic record stores it
  as ``reference_range`` but the FHIR resource calls it ``referenceRange``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.data.models import (
    CodeableConcept,
    Observation,
    ObservationReferenceRange,
    Quantity,
)
from clinical_copilot.tools.fhir_base import FhirBackedTool, reference_id
from clinical_copilot.tools.records import LabRecord


class GetLabsFhirTool(FhirBackedTool):
    name: ClassVar[str] = "get_labs"
    description: ClassVar[str] = (
        "Return the patient's recent lab results (Observation resources). "
        "Use for trends, last-value lookups, and stale-lab detection."
    )
    required_scope: ClassVar[str] = "system/Observation.read"
    record_kind: ClassVar[str] = "Observation"

    async def _fetch(self, *, patient_id: str) -> Sequence[LabRecord]:
        observations = await self._fhir.search_lab_observations(patient_id=patient_id)
        records: list[LabRecord] = []
        for obs in observations:
            record = _project(obs)
            if record is not None:
                records.append(record)
        return records


def _project(obs: Observation) -> LabRecord | None:
    code = obs.code.primary_code() if obs.code else None
    display = obs.code.preferred_display() if obs.code else None
    if not code or not display:
        return None

    value, unit = _value_and_unit(obs)
    if value is None:
        return None

    if not obs.effective_date_time:
        return None

    return LabRecord(
        source_id=reference_id("Observation", obs.id),
        code=code,
        display=display,
        value=value,
        unit=unit,
        observed_on=obs.effective_date_time,
        reference_range=_reference_range(obs.reference_range),
    )


def _value_and_unit(obs: Observation) -> tuple[str | None, str | None]:
    quantity = obs.value_quantity
    if quantity is not None:
        rendered = _render_quantity(quantity)
        if rendered is not None:
            return rendered, quantity.unit
    if obs.value_string:
        return obs.value_string, None
    coded = _render_codeable(obs.value_codeable_concept)
    if coded is not None:
        return coded, None
    return None, None


def _render_quantity(quantity: Quantity) -> str | None:
    if quantity.value is None:
        return None
    # Render integers as integers ("7" not "7.0") so the model surfaces
    # natural lab text. Float() round-trip handles the FHIR "0.9" case
    # without locale-dependent str(float) drift.
    if quantity.value == int(quantity.value):
        return str(int(quantity.value))
    return f"{quantity.value:g}"


def _render_codeable(concept: CodeableConcept | None) -> str | None:
    if concept is None:
        return None
    return concept.preferred_display()


def _reference_range(ranges: list[ObservationReferenceRange]) -> str | None:
    for entry in ranges:
        if entry.text:
            return entry.text
        low = entry.low.value if entry.low else None
        high = entry.high.value if entry.high else None
        if low is not None and high is not None:
            return f"{_fmt_number(low)}-{_fmt_number(high)}"
        if low is not None:
            return f">={_fmt_number(low)}"
        if high is not None:
            return f"<={_fmt_number(high)}"
    return None


def _fmt_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"
