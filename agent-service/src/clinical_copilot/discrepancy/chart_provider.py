"""Chart provider — single seam between the retrieval tools and the engine.

The discrepancy engine takes a :class:`PatientChart` as input. PR 13d's
``get_flags`` swap needs to construct that chart inside the tool's
``_run`` method, but the chart itself can come from any of the data
sources the rest of the tool layer reads from (FixtureStore for the M1
demo, FhirClient for production). Putting the chart-loading behind an
ABC keeps :class:`~clinical_copilot.tools.impl.GetFlagsTool` agnostic to
the source — same rule logic, same flag shape, same verification
contract regardless of where the chart records originated.

Two implementations ship today: :class:`FixtureChartProvider` for the M1
fixture-backed registry, and :class:`FhirChartProvider` for the live
FHIR-backed registry. The FHIR sibling parallelises its six resource
fetches via ``asyncio.gather`` so a cache miss is one bridge round-trip
rather than six sequential tool calls. ACL denials (401 / 403) are
re-raised as :class:`FhirAuthorizationDeniedError` so the upstream
:class:`~clinical_copilot.tools.impl.GetFlagsTool` writes the same
UNAUTHORIZED audit row a JWT-side denial would.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.discrepancy.engine import PatientChart
from clinical_copilot.tools.allergies import project_allergy_intolerance_to_record
from clinical_copilot.tools.base import FhirAuthorizationDeniedError
from clinical_copilot.tools.fhir_base import FHIR_ACL_DENIAL_STATUSES
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.labs import project_observation_to_record
from clinical_copilot.tools.meds import project_medication_request_to_record
from clinical_copilot.tools.notes import project_document_reference_to_record
from clinical_copilot.tools.problems import project_condition_to_record
from clinical_copilot.tools.visits import project_encounter_to_record

if TYPE_CHECKING:
    from collections.abc import Iterable

    from clinical_copilot.data.fhir_client import FhirClient
    from clinical_copilot.runtime.async_bridge import AsyncBridge


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

    Used by the fixture-backed registry and by every test that exercises
    ``get_flags`` against canned data.
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


class FhirChartProvider(ChartProvider):
    """ChartProvider backed by the live :class:`FhirClient`.

    Issues all six resource searches concurrently through the shared
    :class:`AsyncBridge` so a cache-miss cost is one round-trip's worth
    of latency instead of six. Projection reuses the per-tool helpers so
    the records this provider produces are byte-identical to what
    :class:`~clinical_copilot.tools.fhir_base.FhirBackedTool` returns —
    no second source of truth for FHIR→record mapping.

    Failure semantics mirror :class:`FhirBackedTool`:

    * Any :class:`FhirError` aborts the chart load (``asyncio.gather``'s
      default fail-fast cancels in-flight peers). Partial charts would
      give the discrepancy engine a misleadingly complete view.
    * 401 / 403 surface as :class:`FhirAuthorizationDeniedError`, which
      :class:`~clinical_copilot.tools.base.Tool.execute` translates into
      an ``UNAUTHORIZED`` audit row — same shape as the per-tool path.
    * All other :class:`FhirError` subclasses propagate unchanged so the
      orchestrator's ``TOOL_FAILURE`` abstention picks them up.
    """

    def __init__(self, *, fhir: FhirClient, bridge: AsyncBridge) -> None:
        self._fhir = fhir
        self._bridge = bridge

    def load_chart(self, patient_id: str) -> PatientChart:
        try:
            return self._bridge.run(self._load_async(patient_id))
        except FhirError as exc:
            if exc.status_code in FHIR_ACL_DENIAL_STATUSES:
                raise FhirAuthorizationDeniedError(str(exc)) from exc
            raise

    async def _load_async(self, patient_id: str) -> PatientChart:
        conditions, requests, allergies, observations, encounters, documents = await asyncio.gather(
            self._fhir.search_conditions(patient_id=patient_id),
            self._fhir.search_medications(patient_id=patient_id),
            self._fhir.search_allergies(patient_id=patient_id),
            self._fhir.search_lab_observations(patient_id=patient_id),
            self._fhir.search_encounters(patient_id=patient_id),
            self._fhir.search_document_references(patient_id=patient_id),
        )
        return PatientChart(
            patient_id=patient_id,
            problems=tuple(
                _drop_none(project_condition_to_record(c) for c in conditions),
            ),
            medications=tuple(
                _drop_none(project_medication_request_to_record(r) for r in requests),
            ),
            allergies=tuple(
                _drop_none(project_allergy_intolerance_to_record(a) for a in allergies),
            ),
            labs=tuple(
                _drop_none(project_observation_to_record(o) for o in observations),
            ),
            visits=tuple(
                _drop_none(project_encounter_to_record(e) for e in encounters),
            ),
            notes=tuple(
                _drop_none(project_document_reference_to_record(d) for d in documents),
            ),
        )


def _drop_none[RecordT](items: Iterable[RecordT | None]) -> Iterable[RecordT]:
    return (item for item in items if item is not None)
