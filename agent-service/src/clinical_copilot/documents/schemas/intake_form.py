"""``intake_form`` extraction schema (PRD2 §6).

Models a patient-completed intake / history-and-physical questionnaire.
The fields are deliberately coarse: the goal is to surface high-signal
discrepancies against the structured chart (reported allergies vs.
charted allergies; reported current meds vs. charted med list), not to
reproduce the entire form structure.

The schema captures the fields that show up across the demo example
docs: chief complaint, current medications (with RxNorm where the form
prints it), reported allergies, an active-problem list with ICD-10 /
SNOMED codes, multi-relation family history, tobacco status as a
3-state enum (current / former / never) plus optional pack-years, and
a few self-rated scalars (pain, family-history shortcut for the
cardiac-risk question).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from clinical_copilot.documents.schemas.citation import ExtractedField


class TobaccoStatus(StrEnum):
    """Self-reported tobacco-use status (PRD2 §6, intake form)."""

    NEVER = "never"
    FORMER = "former"
    CURRENT = "current"


class SexAssignedAtBirth(StrEnum):
    """Sex assigned at birth as printed on the intake form.

    Values match OpenEMR's ``patient_data.sex`` column convention so
    write-back can pass the value through unchanged. Gender identity
    (a separate concept) is not captured by this schema today; if a
    form distinguishes both, only the legal sex lands here.
    """

    FEMALE = "Female"
    MALE = "Male"
    OTHER = "Other"
    UNKNOWN = "Unknown"


class ReportedMedication(BaseModel):
    """One row of the patient's self-reported current med list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ExtractedField[str]
    dose: ExtractedField[str] | None = None
    frequency: ExtractedField[str] | None = None
    rxnorm: ExtractedField[str] | None = None
    """RxNorm code as printed on the form, when the form provides one
    (the demo Chen / Whitaker intake forms do)."""

    started_year: ExtractedField[int] | None = None
    """Year the patient reports starting the medication."""

    indication: ExtractedField[str] | None = None
    """Free-text reason the patient (or the form's "Reason" column)
    associates with the med (e.g. "Hyperlipidemia", "BPH")."""


class ReportedAllergy(BaseModel):
    """One row of the patient's self-reported allergy list.

    A ``substance`` of "NKDA" or "denies" is treated by the discrepancy
    engine as an explicit no-known-allergies assertion, not as an
    allergen. Negation handling is the responsibility of the extractor;
    this schema only carries strings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    substance: ExtractedField[str]
    reaction: ExtractedField[str] | None = None
    severity: ExtractedField[str] | None = None
    """Free-text severity (e.g. "Mild", "Moderate") as printed."""

    rxnorm: ExtractedField[str] | None = None
    """RxNorm code of the offending drug, when the form prints one."""

    snomed: ExtractedField[str] | None = None
    """SNOMED code of the substance, when the form prints one."""


class ActiveProblem(BaseModel):
    """One row of the patient's self-reported active problem list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition: ExtractedField[str]
    icd10: ExtractedField[str] | None = None
    snomed: ExtractedField[str] | None = None
    onset_year: ExtractedField[int] | None = None
    status: ExtractedField[str] | None = None
    """Free-text status as printed (typically "Active" / "Resolved")."""


class FamilyHistoryEntry(BaseModel):
    """One row of the patient's self-reported family history.

    The Week 1 chart-side has its own family-history representation;
    this is purely the patient-reported shape. Discrepancy detection
    against the chart is part of the W2-08 reconciliation extension.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: ExtractedField[str]
    condition: ExtractedField[str]
    onset_age: ExtractedField[int] | None = None
    status: ExtractedField[str] | None = None
    """Free-text living/deceased status (e.g. "Deceased age 78", "Yes")."""

    snomed: ExtractedField[str] | None = None


class IntakeFormFacts(BaseModel):
    """Top-level extraction result for a single intake form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str

    # ----- Patient demographics (used to pre-populate a new patient
    #       record in the new-patient intake flow). Each field is
    #       optional because not every intake form prints every value;
    #       a missing field means the clinician fills it manually.
    legal_first_name: ExtractedField[str] | None = None
    legal_last_name: ExtractedField[str] | None = None
    date_of_birth: ExtractedField[date] | None = None
    sex_assigned_at_birth: ExtractedField[SexAssignedAtBirth] | None = None
    medical_record_number: ExtractedField[str] | None = None
    """The MRN as printed on the form. Captured for cross-reference
    against the chart side; the agent never trusts a self-reported
    MRN as a write target."""

    phone: ExtractedField[str] | None = None
    email: ExtractedField[str] | None = None

    chief_complaint: ExtractedField[str]
    """Free-text chief complaint as the patient wrote it."""

    current_medications: list[ReportedMedication] = Field(default_factory=list)
    reported_allergies: list[ReportedAllergy] = Field(default_factory=list)
    active_problems: list[ActiveProblem] = Field(default_factory=list)
    family_history: list[FamilyHistoryEntry] = Field(default_factory=list)

    pain_scale: ExtractedField[int] | None = None
    """0..10 self-reported pain. Optional; not all intake forms collect
    one. Range is enforced by the extractor, not the schema, so an
    out-of-range printed value can land as ``OUT_OF_SCHEMA`` rather
    than crashing the parse."""

    tobacco_status: ExtractedField[TobaccoStatus] | None = None
    """Self-reported tobacco-use status. Optional."""

    tobacco_pack_years: ExtractedField[float] | None = None
    """Pack-year history when the form prints one. Captured separately
    from ``tobacco_status`` because "former smoker, 12 pack-years" needs
    both fields to round-trip the patient's history accurately."""
