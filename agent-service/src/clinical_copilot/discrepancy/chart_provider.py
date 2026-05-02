"""Chart provider — single seam between the retrieval tools and the engine.

The discrepancy engine takes a :class:`PatientChart` as input. PR 13d's
``get_flags`` swap needs to construct that chart inside the tool's
``_run`` method, but the chart itself can come from any of the data
sources the rest of the tool layer reads from (FixtureStore for the M1
demo, FhirClient for production). Putting the chart-loading behind an
ABC keeps :class:`~clinical_copilot.tools.impl.GetFlagsTool` agnostic to
the source — same rule logic, same flag shape, same verification
contract regardless of where the chart records originated.

Today only :class:`FixtureChartProvider` ships. PR 14 (cache layer)
adds ``FhirChartProvider`` so the FHIR-backed registry can wire
``get_flags`` too; the registry's current ``from_fhir`` entry-point
deliberately omits ``get_flags`` until that lands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinical_copilot.discrepancy.engine import PatientChart
from clinical_copilot.tools.fixtures import FixtureStore


class ChartProvider(ABC):
    """Loads a :class:`PatientChart` for a specific patient.

    Implementations decide where the records come from (in-memory
    fixture, live FHIR, cached precompute) — the engine and the tool
    above it neither know nor care.
    """

    @abstractmethod
    def load_chart(self, patient_id: str) -> PatientChart:
        """Return the chart the engine should evaluate.

        An unknown ``patient_id`` produces an empty chart (no problems,
        no notes, etc.) — *not* an error. This matches the M1 tool
        layer's contract: an unknown patient is structurally identical
        to "no records of this type" for an in-panel patient, and
        conflating them keeps the abstention surface simple.
        """


class FixtureChartProvider(ChartProvider):
    """ChartProvider backed by the M1 :class:`FixtureStore`.

    Used by the fixture-backed registry today and by every test that
    exercises ``get_flags``. PR 14 adds the FHIR-backed sibling.
    """

    def __init__(self, store: FixtureStore) -> None:
        self._store = store

    def load_chart(self, patient_id: str) -> PatientChart:
        return PatientChart(
            patient_id=patient_id,
            problems=tuple(self._store.problems(patient_id)),
            medications=tuple(self._store.meds(patient_id)),
            allergies=tuple(self._store.allergies(patient_id)),
            labs=tuple(self._store.labs(patient_id)),
            notes=tuple(self._store.notes(patient_id)),
            visits=tuple(self._store.visits(patient_id)),
        )
