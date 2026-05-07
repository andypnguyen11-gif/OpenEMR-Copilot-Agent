"""``fax_tiff`` extraction schema (Week 2 multimodal expansion, Step 2).

Models a multi-page fax packet — typically a referral cover sheet
followed by 3-4 pages of mixed clinical content (referral letter,
lab printouts, intake-form pages). Per-page classification lets the
clinician triage which pages to attach to the chart and which to
ignore; the cover-sheet demographics feed the patient-resolver in
Step 4.

The cohort-5 sample fax packets are 4-5 page bilevel scans at
1700×2200 — already a "real-world messy input" per the PRD2 §1
hard-problem framing, with skewed pages, OCR-degrading compression,
and mixed-content layouts. The schema therefore biases toward
abstaining when a page is illegible rather than guessing wrong.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from clinical_copilot.documents.schemas.citation import ExtractedField


class FaxPageType(StrEnum):
    """Coarse content classification for one page of a fax packet.

    Kept deliberately small: the agent biases toward "other" when
    uncertain rather than mis-classifying as ``lab_report`` and
    triggering a downstream auto-attach to the labs table. The cover
    sheet is a separate type because it's the natural source for the
    sender + recipient + patient demographics.
    """

    COVER = "cover"
    """Routing / metadata page — sender, recipient, patient name, page count."""

    REFERRAL = "referral"
    """Narrative referral letter from a referring provider."""

    LAB_REPORT = "lab_report"
    """A lab-result printout. Eligible for forwarding to ``lab_review.php``."""

    INTAKE_FORM = "intake_form"
    """Patient-completed intake or history form."""

    OTHER = "other"
    """Anything else — imaging report, billing form, illegible page, etc."""


class FaxPage(BaseModel):
    """One page of a multi-page fax packet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: ExtractedField[int]
    """1-indexed page number within the packet."""

    page_type: ExtractedField[FaxPageType]
    """What kind of content the page is. Drives downstream routing."""

    summary: ExtractedField[str]
    """One-sentence description of the page's content. Surfaced on
    the review screen so the clinician can skim before opening
    each page individually."""


class FaxTiffFacts(BaseModel):
    """Top-level extraction result for a single fax-packet TIFF."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    pages: list[FaxPage]

    # Cover-sheet derived fields. All optional because the cover is
    # not always the first page (some senders put the cover last) and
    # because skewed scans frequently lose the printed metadata.
    patient_name: ExtractedField[str] | None = None
    patient_dob: ExtractedField[date] | None = None
    sender_name: ExtractedField[str] | None = None
    fax_date: ExtractedField[date] | None = None
