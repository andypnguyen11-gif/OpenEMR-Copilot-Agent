"""``referral_docx`` extraction schema (Week 2 multimodal expansion, Step 3).

Models a one-page provider-to-provider referral letter authored in
Microsoft Word (.docx). Unlike the lab / intake / fax extractors, this
one is text-only — no VLM call — because the source has structural
tagging (paragraphs, runs) that a deterministic parser handles more
cheaply and reliably than vision OCR.

Citations encode **paragraph index** in the ``page`` slot of
``SourceCitation`` (page=1-based paragraph number for the docx case).
``raw_text`` carries the verbatim paragraph contents, which is what
the review page surfaces. The ``bbox`` is degenerate ``(0, 0, 1, 1)``
since paragraph-level coordinates are meaningless for text-only
content; downstream code already accepts the SourceCitation envelope
unchanged.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField


class ReferralDocxFacts(BaseModel):
    """Top-level extraction result for one referral .docx letter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str

    # ---- Recipient ("TO" provider) ---------------------------------
    recipient_provider: ExtractedField[str] | None = None
    """Name + credentials of the referred-to provider, as printed."""
    recipient_practice: ExtractedField[str] | None = None

    # ---- Referring provider ("FROM") -------------------------------
    referring_provider: ExtractedField[str] | None = None
    referring_provider_npi: ExtractedField[str] | None = None
    referring_practice: ExtractedField[str] | None = None
    referring_phone: ExtractedField[str] | None = None
    referring_fax: ExtractedField[str] | None = None

    # ---- Patient header (RE line) ----------------------------------
    patient_name: ExtractedField[str]
    """The patient the referral is about. Required — a referral letter
    that doesn't name a patient is a parser failure, not abstainable."""
    patient_dob: ExtractedField[date]
    patient_mrn: ExtractedField[str] | None = None

    # ---- Letter metadata --------------------------------------------
    letter_date: ExtractedField[date] | None = None

    # ---- Clinical narrative -----------------------------------------
    reason_for_referral: ExtractedField[str]
    """Required — this is the load-bearing field the receiving provider
    needs to triage the referral."""

    history_summary: ExtractedField[str] | None = None
    """The HPI / clinical narrative. Optional because not every
    referral letter includes one (some are bare-bones)."""

    requested_action: ExtractedField[str] | None = None
    """The "Specific Question / Requested Action" section."""

    # ---- Lists -------------------------------------------------------
    past_medical_history: list[ExtractedField[str]] = []
    current_medications: list[ExtractedField[str]] = []
    pertinent_labs: list[ExtractedField[str]] = []

    allergies: ExtractedField[str] | None = None
    """Allergies render as a single line in the cohort-5 corpus
    (e.g. "NKDA" or "Sulfa drugs — rash"). Lift to a list of
    ``ReportedAllergy`` if a future referral source breaks them out."""
