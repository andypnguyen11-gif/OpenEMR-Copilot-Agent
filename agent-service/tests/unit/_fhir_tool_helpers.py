"""Shared scaffolding for the six PR-8 FHIR-backed tool unit tests.

The per-tool ``test_tools_*.py`` files share three concerns:

* A stub :class:`FhirClient` whose async search methods return canned
  Pydantic models (or raise :class:`FhirError` to drive denial / fault
  paths). Subclassing rather than mocking keeps the type checker happy
  and makes the contract explicit at the call site.
* An :class:`AsyncBridge` for the test session. One loop / thread is
  enough — the bridge is reusable across tests and tears down at module
  exit via the ``bridge`` fixture.
* A recording :class:`AuditLogWriter` so the per-tool tests can assert
  on the UNAUTHORIZED audit row written when the FHIR ACL denial path
  fires (matching the contract pinned in
  ``tests/unit/test_tool_rbac.py``).

Underscore prefix on the filename so pytest's default discovery does
not collect it as a test module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.data.fhir_client import FhirClient, FhirError
from clinical_copilot.data.models import (
    AllergyIntolerance,
    Condition,
    DocumentReference,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
)
from clinical_copilot.tools.records import AnyRecord

_StubResource = TypeVar(
    "_StubResource",
    AllergyIntolerance,
    Condition,
    DocumentReference,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
)

AUDIT_SALT = "test-salt"
PATIENT_ID = "p101"
ALL_SCOPES = [
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
]


class RecordingAuditWriter(AuditLogWriter):
    """In-memory drop-in for the production writer.

    Subclassing the production class keeps the type narrow at every
    call site that consumes :class:`AuditLogWriter` — same pattern as
    the existing ``test_tools.py`` recorder.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class StubFhirClient(FhirClient):
    """Test double for :class:`FhirClient`.

    Each method delegates to a per-resource handler the test supplies.
    Default handlers return ``[]`` so a tool that fans out to multiple
    resources doesn't trip on an unset stub. Setting ``raise_status`` on
    a handler to a 4xx code causes the matching method to raise a
    :class:`FhirError` with that status — drives the ACL-denial path
    and the non-ACL fault path uniformly.

    The constructor deliberately bypasses the real :class:`FhirClient`
    init (no OAuth, no httpx.AsyncClient) — none of those collaborators
    are exercised by the projection logic under test.
    """

    def __init__(
        self,
        *,
        conditions: Callable[..., list[Condition]] | FhirError | None = None,
        medications: Callable[..., list[MedicationRequest]] | FhirError | None = None,
        allergies: Callable[..., list[AllergyIntolerance]] | FhirError | None = None,
        labs: Callable[..., list[Observation]] | FhirError | None = None,
        encounters: Callable[..., list[Encounter]] | FhirError | None = None,
        documents: Callable[..., list[DocumentReference]] | FhirError | None = None,
    ) -> None:
        # Skip super().__init__ on purpose — we do not want the real
        # FhirClient construction (which requires a working OAuth and
        # AsyncClient) on the test path.
        self.calls: list[tuple[str, str]] = []
        self._conditions = conditions
        self._medications = medications
        self._allergies = allergies
        self._labs = labs
        self._encounters = encounters
        self._documents = documents

    async def search_conditions(self, *, patient_id: str) -> list[Condition]:
        return self._dispatch("conditions", patient_id, self._conditions, Condition)

    async def search_medications(self, *, patient_id: str) -> list[MedicationRequest]:
        return self._dispatch("medications", patient_id, self._medications, MedicationRequest)

    async def search_allergies(self, *, patient_id: str) -> list[AllergyIntolerance]:
        return self._dispatch("allergies", patient_id, self._allergies, AllergyIntolerance)

    async def search_lab_observations(self, *, patient_id: str) -> list[Observation]:
        return self._dispatch("labs", patient_id, self._labs, Observation)

    async def search_encounters(self, *, patient_id: str) -> list[Encounter]:
        return self._dispatch("encounters", patient_id, self._encounters, Encounter)

    async def search_document_references(self, *, patient_id: str) -> list[DocumentReference]:
        return self._dispatch("documents", patient_id, self._documents, DocumentReference)

    async def get_patient(self, patient_id: str) -> Patient:  # pragma: no cover
        # PR 8 tools never call get_patient; surfacing a clear failure
        # if a future tool does keeps the stub honest.
        raise NotImplementedError("StubFhirClient does not implement get_patient")

    def _dispatch(
        self,
        kind: str,
        patient_id: str,
        handler: Callable[..., list[_StubResource]] | FhirError | None,
        _resource_type: type[_StubResource],
    ) -> list[_StubResource]:
        self.calls.append((kind, patient_id))
        if isinstance(handler, FhirError):
            raise handler
        if handler is None:
            return []
        return handler(patient_id=patient_id)


def claims_for(
    patient_id: str = PATIENT_ID,
    *,
    scopes: list[str] | None = None,
) -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role="physician",
        patient_id=patient_id,
        scopes=scopes if scopes is not None else ALL_SCOPES,
        nonce="n-test",
        jti=f"jti-{patient_id}",
    )


def expect_record[T: AnyRecord](record: AnyRecord, kind: type[T]) -> T:
    """Narrow an :class:`AnyRecord` to its concrete subtype.

    Tests access kind-specific fields (``substance``, ``onset_date``,
    etc.) that aren't part of the union surface, so mypy's strict mode
    flags the access on a bare :class:`AnyRecord`. This helper asserts
    the runtime type and returns the narrowed record so the rest of the
    test reads the field on the concrete class.
    """

    assert isinstance(record, kind), f"expected {kind.__name__}, got {type(record).__name__}"
    return record
