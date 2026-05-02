"""Fixture loader for the M1 tool layer.

Loads ``tests/fixtures/patients.json`` once at process start and exposes
typed accessors per resource type. PR 6 replaces this with a live FHIR
client; the orchestrator and Tool ABC don't know which one is wired in.

Validation is strict: any drift between the JSON and the Pydantic record
shapes raises at startup, not at tool-call time. That's the behavior we
want — a malformed fixture is a deploy-time error, not a runtime mystery.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from clinical_copilot.tools.records import (
    AllergyRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
    VisitRecord,
)

_DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "patients.json"


class FixtureStoreError(RuntimeError):
    """Raised when the fixture is missing, malformed, or asks for an
    unknown patient. Tools translate the unknown-patient case into an
    empty record list rather than letting it bubble — a missing patient
    is structurally identical to "no records of this type" for an
    in-panel patient, and conflating them keeps the abstention surface
    simple. The malformed-fixture case is a deploy-time error.
    """


_PROBLEM_LIST = TypeAdapter(list[ProblemRecord])
_MED_LIST = TypeAdapter(list[MedicationRecord])
_ALLERGY_LIST = TypeAdapter(list[AllergyRecord])
_LAB_LIST = TypeAdapter(list[LabRecord])
_VISIT_LIST = TypeAdapter(list[VisitRecord])
_NOTE_LIST = TypeAdapter(list[NoteRecord])


class FixtureStore:
    """Read-only view over the JSON fixture.

    Accessor methods return frozen Pydantic models. An unknown
    ``patient_id`` returns an empty list — *not* an error — so a
    correctly-scoped tool call against a patient with no records of that
    type produces the same surface as a missing-data abstention.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        patients = payload.get("patients")
        if not isinstance(patients, dict):
            raise FixtureStoreError("fixture missing 'patients' object")
        self._patients: dict[str, dict[str, object]] = {
            str(pid): _coerce_patient_block(pid, block) for pid, block in patients.items()
        }

    @classmethod
    def from_file(cls, path: Path | None = None) -> FixtureStore:
        target = path if path is not None else _DEFAULT_FIXTURE
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise FixtureStoreError(f"cannot read fixture {target}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FixtureStoreError(f"fixture {target} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise FixtureStoreError(f"fixture {target} root must be a JSON object")
        return cls(payload)

    def has_patient(self, patient_id: str) -> bool:
        return patient_id in self._patients

    def problems(self, patient_id: str) -> list[ProblemRecord]:
        return _PROBLEM_LIST.validate_python(self._slice(patient_id, "problems"))

    def meds(self, patient_id: str) -> list[MedicationRecord]:
        return _MED_LIST.validate_python(self._slice(patient_id, "meds"))

    def allergies(self, patient_id: str) -> list[AllergyRecord]:
        return _ALLERGY_LIST.validate_python(self._slice(patient_id, "allergies"))

    def labs(self, patient_id: str) -> list[LabRecord]:
        return _LAB_LIST.validate_python(self._slice(patient_id, "labs"))

    def visits(self, patient_id: str) -> list[VisitRecord]:
        return _VISIT_LIST.validate_python(self._slice(patient_id, "visits"))

    def notes(self, patient_id: str) -> list[NoteRecord]:
        return _NOTE_LIST.validate_python(self._slice(patient_id, "notes"))

    def _slice(self, patient_id: str, key: str) -> list[object]:
        block = self._patients.get(patient_id)
        if block is None:
            return []
        rows = block.get(key)
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise FixtureStoreError(f"patient {patient_id!r}: expected list at {key!r}")
        return rows


_EXPECTED_BLOCK_KEYS = (
    "demographics",
    "problems",
    "meds",
    "allergies",
    "labs",
    "visits",
    "notes",
)


def _coerce_patient_block(patient_id: object, block: object) -> dict[str, object]:
    if not isinstance(block, dict):
        raise FixtureStoreError(f"patient {patient_id!r}: block must be a JSON object")
    for key in _EXPECTED_BLOCK_KEYS:
        if key not in block:
            raise FixtureStoreError(f"patient {patient_id!r}: missing key {key!r}")
    return block
