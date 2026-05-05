"""``lab_pdf`` extraction schema (PRD2 §6).

Models a lab report as a list of Observation-shaped extracted fields.
Each observation carries the LOINC-or-name code, the numeric value with
units, the effective date, and (optionally) the reference range — every
one of those wrapped in ``ExtractedField`` so a single missing value
does not invalidate the whole document.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField


class LabObservation(BaseModel):
    """One row of a lab panel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ExtractedField[str]
    """LOINC code if recognized, otherwise the printed analyte name."""

    display: ExtractedField[str]
    """Human-readable analyte name as printed on the report."""

    value: ExtractedField[float]
    """Numeric value of the observation."""

    unit: ExtractedField[str]
    """Unit of measure as printed (e.g. ``mg/dL``)."""

    effective_date: ExtractedField[date]
    """Specimen-collection date if printed, else report date."""

    reference_low: ExtractedField[float] | None = None
    reference_high: ExtractedField[float] | None = None
    """Reference-range bounds; either may be absent on the printed
    report. Whole field is ``None`` when the report omits the range."""

    flag: ExtractedField[str] | None = None
    """Lab-printed flag (``H``, ``L``, ``HH``, ``LL``, ``A``). Optional;
    not every report prints one."""


class LabPdfFacts(BaseModel):
    """Top-level extraction result for a single lab PDF."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    observations: list[LabObservation]
