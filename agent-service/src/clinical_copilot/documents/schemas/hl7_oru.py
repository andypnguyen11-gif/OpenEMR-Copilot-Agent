"""``hl7_oru`` extraction schema (Week 2 multimodal expansion, Step 6).

Models an HL7 v2 ORU-R01 message — the standard "Observation Result"
message hospitals and reference labs send to push lab values into an
EHR. Unlike the lab PDF extractor (which works from a printed report),
this extractor works from the structured pipe-delimited stream, so
codes (LOINC) and units come back deterministically.

The schema reuses :class:`LabObservation` from ``lab_pdf`` because the
downstream confirm flow (``lab_review.php``) renders the same shape.
The HL7-specific facts class adds the patient demographics carried
in the PID segment so the patient resolver can match the message to
an existing chart.

Citation shape: ``page`` is the 1-indexed segment number (the PID
segment is page 2, an OBX is page 6, etc.), ``raw_text`` carries the
verbatim segment text, ``bbox`` is degenerate ``(0, 0, 1, 1)``.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField
from clinical_copilot.documents.schemas.lab_pdf import LabObservation


class Hl7OruFacts(BaseModel):
    """Top-level extraction result for one HL7 v2 ORU-R01 message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str

    # PID-derived demographics (always present in a well-formed ORU
    # because PID is a required segment in the ORU_R01 trigger event).
    patient_name: ExtractedField[str]
    patient_dob: ExtractedField[date] | None = None
    patient_mrn: ExtractedField[str] | None = None
    patient_sex: ExtractedField[str] | None = None

    # OBR-derived order metadata (one ORU can carry multiple OBRs but
    # the cohort-5 corpus has one OBR per file — Step 6 handles the
    # single-OBR case explicitly).
    order_panel: ExtractedField[str] | None = None
    """The OBR-4 universal-service-identifier display name (e.g.
    'Lipid panel with direct LDL - Serum or Plasma')."""

    order_loinc: ExtractedField[str] | None = None
    """The OBR-4 LOINC code if the universal service identifier
    namespace is LN."""

    specimen_collected_at: ExtractedField[date] | None = None
    """OBR-7 specimen collection datetime → date."""

    ordering_provider: ExtractedField[str] | None = None
    """OBR-16 ordering provider name."""

    # Each OBX segment becomes one observation. Reuses LabObservation
    # so the existing lab_review.php / lab_save_ai.php confirm flow
    # handles ORU results identically to lab-PDF observations.
    observations: list[LabObservation]
