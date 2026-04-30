"""Typed record schemas returned by each tool.

The orchestrator's verification middleware joins ``source_id`` strings the
model writes into ``prose[]`` against the ``source_id`` field of these
records. That join is the keystone of the trust story (citation-existence
check), so every record carries a stable, server-issued ``source_id`` and
the field is the first one in every model.

Schema stability across PRs is part of the contract — PR 6 swaps the fixture
for live FHIR and these models do not change. Add fields by appending; do
not rename.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProblemRecord(_Record):
    source_id: str = Field(min_length=1)
    code: str
    display: str
    onset_date: str | None = None
    status: str


class MedicationRecord(_Record):
    source_id: str = Field(min_length=1)
    name: str
    dose: str | None = None
    status: str
    started_on: str | None = None


class AllergyRecord(_Record):
    source_id: str = Field(min_length=1)
    substance: str
    reaction: str | None = None
    severity: str | None = None


class LabRecord(_Record):
    source_id: str = Field(min_length=1)
    code: str
    display: str
    value: str
    unit: str | None = None
    observed_on: str
    reference_range: str | None = None


class VisitRecord(_Record):
    source_id: str = Field(min_length=1)
    encounter_type: str
    visited_on: str
    chief_complaint: str | None = None


class NoteRecord(_Record):
    source_id: str = Field(min_length=1)
    note_date: str
    author: str
    body: str


class FlagRecord(_Record):
    source_id: str = Field(min_length=1)
    rule_id: str
    category: str
    rationale: str
    referenced_source_ids: list[str]


type AnyRecord = (
    ProblemRecord
    | MedicationRecord
    | AllergyRecord
    | LabRecord
    | VisitRecord
    | NoteRecord
    | FlagRecord
)


class ToolResult(_Record):
    """Wrapper a tool returns to the orchestrator.

    The orchestrator passes the wrapper's ``records`` to the model as
    delimited tool-call output, and the verification middleware uses
    ``records`` to resolve every cited ``source_id``. ``patient_id`` is
    echoed back so the middleware can sanity-check that the tool was
    scoped to the claimed patient.
    """

    tool_name: str
    patient_id: str
    records: list[AnyRecord]
