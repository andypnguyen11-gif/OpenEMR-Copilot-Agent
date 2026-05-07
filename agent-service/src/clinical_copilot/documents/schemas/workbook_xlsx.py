"""``workbook_xlsx`` extraction schema (Week 2 multimodal expansion, Step 5).

Models a multi-sheet Excel workbook of patient information — typically
exported from a registry / pre-visit-prep tool. The cohort-5 layout is
four sheets:

  * ``Patient`` — vertical key/value of demographics + insurance + PCP.
  * ``Medications`` — tabular: brand, generic, strength, sig, indication,
    dates, prescriber.
  * ``Labs_Trend`` — date-pivoted (rows = test, columns = report dates).
    The extractor unpivots to a flat list so each ``WorkbookLabReading``
    is one (test, date, value) triple.
  * ``Care_Gaps`` — tabular: HEDIS / USPSTF measure rows with status
    (UP TO DATE / OVERDUE), last-done, due-date, notes.

Citations encode the cell coordinate in the existing ``SourceCitation``
envelope: ``page`` is the 1-indexed sheet number, ``raw_text`` carries
the ``Sheet!Cell`` reference (e.g. ``Patient!B2``), and the ``bbox`` is
the degenerate ``(0, 0, 1, 1)`` because cells don't have meaningful
pixel coordinates in a flow-laid-out spreadsheet.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField


class WorkbookPatientInfo(BaseModel):
    """Demographics block from the ``Patient`` sheet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ExtractedField[str]
    """Required — a workbook with no patient name is a parser failure."""

    dob: ExtractedField[date] | None = None
    sex: ExtractedField[str] | None = None
    mrn: ExtractedField[str] | None = None
    pcp: ExtractedField[str] | None = None
    pcp_npi: ExtractedField[str] | None = None
    phone: ExtractedField[str] | None = None
    address: ExtractedField[str] | None = None
    insurance: ExtractedField[str] | None = None
    allergies: ExtractedField[str] | None = None


class WorkbookMedication(BaseModel):
    """One row of the ``Medications`` sheet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generic: ExtractedField[str]
    """Required — brand-only entries are downgraded to brand=as-generic
    rather than emitted with no generic field."""

    brand: ExtractedField[str] | None = None
    strength: ExtractedField[str] | None = None
    sig: ExtractedField[str] | None = None
    indication: ExtractedField[str] | None = None
    prescriber: ExtractedField[str] | None = None


class WorkbookLabReading(BaseModel):
    """One (test, date, value) triple from the unpivoted ``Labs_Trend``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test: ExtractedField[str]
    value: ExtractedField[float]
    reading_date: ExtractedField[date]
    loinc: ExtractedField[str] | None = None
    unit: ExtractedField[str] | None = None
    reference_range: ExtractedField[str] | None = None


class WorkbookCareGap(BaseModel):
    """One row of the ``Care_Gaps`` sheet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measure: ExtractedField[str]
    status: ExtractedField[str]
    """Free-form per the source ("UP TO DATE", "OVERDUE", etc.).
    Downstream code matches on uppercase prefixes rather than parsing
    into an enum so a future status value (e.g. "DUE SOON") doesn't
    crash the extractor."""

    last_done: ExtractedField[date] | None = None
    due_date: ExtractedField[date] | None = None
    notes: ExtractedField[str] | None = None


class WorkbookXlsxFacts(BaseModel):
    """Top-level extraction result for a patient workbook .xlsx."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    patient: WorkbookPatientInfo
    medications: list[WorkbookMedication]
    lab_readings: list[WorkbookLabReading]
    care_gaps: list[WorkbookCareGap]
