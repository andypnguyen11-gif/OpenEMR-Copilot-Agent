"""``hl7_adt`` extraction schema (Week 2 multimodal expansion, Step 7).

Models an HL7 v2 ADT-A08 (Update Patient Information) message — the
standard "demographics changed" trigger event a hospital registration
system sends downstream so receivers can refresh their patient
record. Cohort-5 ADT messages also carry the chart-update reason in
the EVN-7 segment, the primary-care provider in PD1, the next-of-kin
in NK1, and insurance info in IN1.

The downstream confirm flow lives in the document review page (Step
4): the clinician sees the extracted demographics + the agent's
suggested patient match, then confirms an existing chart or triggers
the create-new-patient workflow. This extractor never auto-writes.

Citation shape: same convention as the ORU extractor — ``page`` is
the 1-indexed segment number, ``raw_text`` carries the verbatim
segment text, ``bbox`` is degenerate ``(0, 0, 1, 1)``.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField


class Hl7AdtFacts(BaseModel):
    """Top-level extraction result for one HL7 v2 ADT-A08 message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str

    # PID-derived demographics — required for an ADT message because
    # PID is the load-bearing segment of the trigger event.
    patient_name: ExtractedField[str]
    patient_dob: ExtractedField[date] | None = None
    patient_mrn: ExtractedField[str] | None = None
    patient_sex: ExtractedField[str] | None = None
    patient_address: ExtractedField[str] | None = None
    patient_phone: ExtractedField[str] | None = None
    patient_race: ExtractedField[str] | None = None

    # EVN-7 — the human-readable reason this update was sent.
    update_reason: ExtractedField[str] | None = None

    # PD1-4 — primary care provider (NPI^LAST^FIRST^MIDDLE).
    primary_care_provider: ExtractedField[str] | None = None
    primary_care_provider_npi: ExtractedField[str] | None = None

    # NK1 — first emergency contact / next of kin.
    next_of_kin_name: ExtractedField[str] | None = None
    next_of_kin_relationship: ExtractedField[str] | None = None
    next_of_kin_phone: ExtractedField[str] | None = None

    # IN1 — primary insurance.
    insurance_carrier: ExtractedField[str] | None = None
    insurance_plan_id: ExtractedField[str] | None = None
    insurance_member_id: ExtractedField[str] | None = None
    insurance_group_number: ExtractedField[str] | None = None
