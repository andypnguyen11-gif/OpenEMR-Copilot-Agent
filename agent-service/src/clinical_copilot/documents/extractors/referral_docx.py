"""DOCX referral-letter extractor (Week 2 multimodal expansion, Step 3).

Reads a one-page provider-to-provider referral letter authored in
Microsoft Word and produces a :class:`ReferralDocxFacts` model with
per-paragraph citations. The cohort-5 referrals follow a stable
template (sender header → recipient header → ``RE:`` patient line →
salutation → labelled sections → sign-off), so the parser works by
keyword-matching paragraph prefixes rather than calling a VLM.

Citation shape: each ``SourceCitation`` carries
``page=<paragraph_index_1based>`` (the docx ``page`` slot is
overloaded to encode paragraph position because referral letters are
single-page documents) and ``raw_text=<paragraph_contents>``. The
``bbox`` is degenerate ``(0, 0, 1, 1)`` since paragraphs do not
have meaningful pixel coordinates in a flow-laid-out document.

This extractor is text-only (no Anthropic call). Cost: ~zero per
case; latency: a few milliseconds. The downside is that the parser
is brittle to template drift — a referral source that prints
sections in a different order or with different labels will land
fields in ``abstain_reason=NO_DATA``. That tradeoff is correct for
the cohort-5 demo set; production code would either add a per-template
parser registry or fall back to a VLM pass when the keyword pass
yields too many abstentions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from docx import Document
from docx.document import Document as DocxDocument

from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.referral_docx import ReferralDocxFacts
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

# ---------------------------------------------------------------------
# Section header keywords. Matched as a prefix on a paragraph (after
# stripping leading whitespace + the colon) so we tolerate the small
# formatting variations real templates introduce.
# ---------------------------------------------------------------------

_SECTION_PATTERNS = {
    "reason_for_referral": re.compile(r"^Reason for Referral\s*[:\-]\s*", re.I),
    "history_summary": re.compile(r"^History of Present Illness\s*[:\-]\s*", re.I),
    "past_medical_history": re.compile(r"^Past Medical History\s*[:\-]?\s*$", re.I),
    "current_medications": re.compile(r"^Current Medications\s*[:\-]?\s*$", re.I),
    "allergies": re.compile(r"^Allergies\s*[:\-]\s*", re.I),
    "pertinent_labs": re.compile(r"^Pertinent Labs?\s*[:\-]?\s*$", re.I),
    "requested_action": re.compile(
        r"^(?:Specific Question|Requested Action|Specific Question\s*/\s*Requested Action)\s*[:\-]\s*",
        re.I,
    ),
}

# Patient header regex — the cohort-5 template is reliably
# "RE: NAME | DOB: MM/DD/YYYY | MRN: MRN_VALUE". Allows a single
# space variant around the bars.
_RE_PATIENT_HEADER = re.compile(
    r"^RE\s*[:\-]\s*(?P<name>[^|]+?)\s*\|\s*"
    r"DOB\s*[:\-]\s*(?P<dob>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*\|\s*"
    r"MRN\s*[:\-]\s*(?P<mrn>\S+)\s*$",
    re.I,
)

# A docx "letter date" line — looks like "May 6, 2026" or "5/6/2026".
_RE_LETTER_DATE = re.compile(
    r"^(?:"
    r"(?P<long>(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4})"
    r"|"
    r"(?P<short>\d{1,2}[/-]\d{1,2}[/-]\d{4})"
    r")\s*$"
)

_RE_NPI = re.compile(r"NPI\s*[:\-]\s*(?P<npi>\d{10})", re.I)
_RE_PHONE = re.compile(r"Phone\s*[:\-]\s*(?P<phone>[^\s].*?)(?:\s*Fax|$)", re.I)
_RE_FAX = re.compile(r"Fax\s*[:\-]\s*(?P<fax>[^\s].*?)\s*$", re.I)
_RE_DEAR = re.compile(r"^Dear\s+", re.I)


@dataclass(frozen=True, slots=True)
class _Paragraph:
    """One paragraph as far as the extractor cares about it."""

    index: int  # 1-based for citation use
    text: str


def extract_referral_docx(
    *,
    client: Anthropic,  # unused; kept for signature uniformity
    model: str,  # unused; kept for signature uniformity
    document_id: str,
    document_path: Path,
) -> ReferralDocxFacts:
    """Public registry entry point. Signature matches the other
    extractors so the registry dispatch is uniform."""

    del client, model  # this extractor is deterministic / text-only
    doc: DocxDocument = Document(str(document_path))
    paragraphs: list[_Paragraph] = [
        _Paragraph(index=i + 1, text=p.text.strip())
        for i, p in enumerate(doc.paragraphs)
        if p.text.strip()
    ]
    if not paragraphs:
        raise ValueError(f"docx is empty / contains no text paragraphs: {document_path}")

    # First pass: locate the patient header line — its index splits the
    # document into "letterhead block" (above) and "body" (below).
    patient_para = _find_patient_header(paragraphs)
    if patient_para is None:
        raise ValueError(
            f"could not find 'RE: NAME | DOB: ... | MRN: ...' header in {document_path}"
        )

    header_block = [p for p in paragraphs if p.index < patient_para.index]
    body_block = [p for p in paragraphs if p.index > patient_para.index]

    patient_match = _RE_PATIENT_HEADER.match(patient_para.text)
    assert patient_match is not None  # _find_patient_header just verified this

    patient_cite = _cite(document_id, patient_para)
    patient_name_field: ExtractedField[str] = ExtractedField[str](
        value=patient_match["name"].strip(),
        citation=patient_cite,
    )
    parsed_dob = _parse_dob(patient_match["dob"])
    patient_dob_field: ExtractedField[date]
    if parsed_dob is not None:
        patient_dob_field = ExtractedField[date](value=parsed_dob, citation=patient_cite)
    else:
        patient_dob_field = ExtractedField[date](
            abstain_reason=RuntimeAbstainReason.OUT_OF_SCHEMA
        )
    patient_mrn_field: ExtractedField[str] | None = ExtractedField[str](
        value=patient_match["mrn"].strip(),
        citation=patient_cite,
    )

    # Header block: first paragraph is usually the sender practice
    # name. Date line and recipient block follow. Recipient is the
    # contiguous run of paragraphs above the patient header that does
    # NOT contain the sender phone/fax — a coarse heuristic that works
    # for the cohort-5 template.
    referring_practice_field, referring_phone_field, referring_fax_field = (
        _extract_sender_block(document_id, header_block)
    )
    letter_date_field = _extract_letter_date(document_id, header_block)
    recipient_provider_field, recipient_practice_field = _extract_recipient_block(
        document_id, header_block
    )

    # Sign-off block: paragraphs after "Sincerely," up to the
    # synthetic-data footer. The first non-empty line is the
    # sender's printed name + credentials.
    sign_off = _find_sign_off(body_block)
    referring_provider_field = None
    referring_provider_npi_field = None
    if sign_off is not None:
        referring_provider_field = ExtractedField[str](
            value=sign_off.text,
            citation=_cite(document_id, sign_off),
        )
        # NPI line is usually 1-2 paragraphs after the name.
        for p in body_block:
            if p.index <= sign_off.index:
                continue
            if (m := _RE_NPI.search(p.text)) is not None:
                referring_provider_npi_field = ExtractedField[str](
                    value=m["npi"],
                    citation=_cite(document_id, p),
                )
                break

    # Sectioned content. We walk body_block, classify each paragraph
    # by whether it's a section header, a list item under a section,
    # or a free-text section content paragraph immediately following
    # an inline-labelled header.
    sections = _split_sections(body_block)

    reason_for_referral_field = _build_text_field(
        document_id,
        sections.get("reason_for_referral"),
        required=True,
    )
    history_summary_field = _build_text_field(
        document_id, sections.get("history_summary"), required=False
    )
    requested_action_field = _build_text_field(
        document_id, sections.get("requested_action"), required=False
    )
    allergies_field = _build_text_field(
        document_id, sections.get("allergies"), required=False
    )
    pmh_list = _build_list(document_id, sections.get("past_medical_history") or [])
    meds_list = _build_list(document_id, sections.get("current_medications") or [])
    labs_list = _build_list(document_id, sections.get("pertinent_labs") or [])

    return ReferralDocxFacts(
        document_id=document_id,
        recipient_provider=recipient_provider_field,
        recipient_practice=recipient_practice_field,
        referring_provider=referring_provider_field,
        referring_provider_npi=referring_provider_npi_field,
        referring_practice=referring_practice_field,
        referring_phone=referring_phone_field,
        referring_fax=referring_fax_field,
        patient_name=patient_name_field,
        patient_dob=patient_dob_field,
        patient_mrn=patient_mrn_field,
        letter_date=letter_date_field,
        reason_for_referral=reason_for_referral_field,
        history_summary=history_summary_field,
        requested_action=requested_action_field,
        past_medical_history=pmh_list,
        current_medications=meds_list,
        pertinent_labs=labs_list,
        allergies=allergies_field,
    )


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _cite(document_id: str, para: _Paragraph) -> SourceCitation:
    """Build a SourceCitation for a docx paragraph.

    ``page`` is overloaded to carry the 1-based paragraph index;
    ``bbox`` is degenerate (full unit square) because paragraph-level
    pixel coordinates do not exist for text-only docx content.
    """

    return SourceCitation(
        document_id=document_id,
        page=para.index,
        bbox=(0.0, 0.0, 1.0, 1.0),
        confidence=1.0,
        raw_text=para.text,
    )


def _find_patient_header(paragraphs: list[_Paragraph]) -> _Paragraph | None:
    for p in paragraphs:
        if _RE_PATIENT_HEADER.match(p.text):
            return p
    return None


def _parse_dob(raw: str) -> date | None:
    """Accept MM/DD/YYYY, M/D/YYYY, MM/DD/YY, etc."""

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_letter_date(raw: str) -> date | None:
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _extract_letter_date(
    document_id: str, header_block: list[_Paragraph]
) -> ExtractedField[date] | None:
    for p in header_block:
        if (m := _RE_LETTER_DATE.match(p.text)) is not None:
            raw = m["long"] or m["short"] or ""
            parsed = _parse_letter_date(raw)
            if parsed is not None:
                return ExtractedField[date](value=parsed, citation=_cite(document_id, p))
    return None


def _extract_sender_block(
    document_id: str, header_block: list[_Paragraph]
) -> tuple[
    ExtractedField[str] | None,
    ExtractedField[str] | None,
    ExtractedField[str] | None,
]:
    """First paragraph is the sender practice; the contact line(s)
    that contain Phone:/Fax: are the sender's contact info because
    in the cohort-5 template the recipient block doesn't repeat them
    in the same paragraph as the sender's letterhead."""

    practice: ExtractedField[str] | None = None
    phone: ExtractedField[str] | None = None
    fax: ExtractedField[str] | None = None

    for i, p in enumerate(header_block):
        if i == 0 and not _RE_LETTER_DATE.match(p.text):
            practice = ExtractedField[str](value=p.text, citation=_cite(document_id, p))
        if (m := _RE_PHONE.search(p.text)) is not None and phone is None:
            phone = ExtractedField[str](
                value=m["phone"].strip(),
                citation=_cite(document_id, p),
            )
        if (m := _RE_FAX.search(p.text)) is not None and fax is None:
            fax = ExtractedField[str](
                value=m["fax"].strip(),
                citation=_cite(document_id, p),
            )
    return practice, phone, fax


def _extract_recipient_block(
    document_id: str, header_block: list[_Paragraph]
) -> tuple[ExtractedField[str] | None, ExtractedField[str] | None]:
    """Recipient block sits between the letter date and the patient
    header. The first non-date paragraph after the date is the
    recipient provider name; the next non-contact paragraph is the
    recipient practice name."""

    date_idx = -1
    for i, p in enumerate(header_block):
        if _RE_LETTER_DATE.match(p.text):
            date_idx = i
            break
    if date_idx == -1:
        return None, None

    recipient_provider: ExtractedField[str] | None = None
    recipient_practice: ExtractedField[str] | None = None
    for p in header_block[date_idx + 1 :]:
        text = p.text
        if _RE_PHONE.search(text) or _RE_FAX.search(text) or _RE_NPI.search(text):
            continue  # skip the recipient's contact lines
        if recipient_provider is None:
            recipient_provider = ExtractedField[str](value=text, citation=_cite(document_id, p))
        elif recipient_practice is None:
            recipient_practice = ExtractedField[str](value=text, citation=_cite(document_id, p))
            break
    return recipient_provider, recipient_practice


def _find_sign_off(body_block: list[_Paragraph]) -> _Paragraph | None:
    """Find the sender name. Cohort-5 template: paragraph after
    "Sincerely," (which is itself a paragraph)."""

    for i, p in enumerate(body_block):
        if p.text.lower().rstrip(",.") == "sincerely":
            for q in body_block[i + 1 :]:
                if q.text and not _RE_PHONE.search(q.text) and not _RE_FAX.search(q.text):
                    return q
            return None
    return None


@dataclass(frozen=True, slots=True)
class _SectionHit:
    """A section header paragraph + its inline content (if any) +
    the contiguous run of subsequent paragraphs that belong to it."""

    header: _Paragraph
    inline_content: str  # the text after the section label on the same line
    items: list[_Paragraph]  # paragraphs between this section and the next


def _split_sections(body_block: list[_Paragraph]) -> dict[str, _SectionHit | list[_Paragraph]]:
    """Walk paragraphs, classify each as a section header (and capture
    the inline content + subsequent items) or a section item.

    Returns a dict keyed by section name. For sections whose content is
    inline (Reason for Referral, History, Allergies, Requested Action),
    the value is a single ``_SectionHit`` whose ``inline_content`` is
    the load-bearing text. For list sections (PMH, Medications, Labs),
    the value is a list of item paragraphs.
    """

    out: dict[str, _SectionHit | list[_Paragraph]] = {}

    current_section: str | None = None
    current_items: list[_Paragraph] = []

    def _flush() -> None:
        nonlocal current_section, current_items
        if current_section is not None and current_items:
            existing = out.get(current_section)
            if isinstance(existing, _SectionHit):
                existing.items.extend(current_items)
            else:
                # Pure list section.
                out[current_section] = current_items.copy()
        current_items = []

    for p in body_block:
        # Detect section header.
        matched_name: str | None = None
        for name, pattern in _SECTION_PATTERNS.items():
            if pattern.match(p.text):
                matched_name = name
                break

        if matched_name is not None:
            _flush()
            inline = _SECTION_PATTERNS[matched_name].sub("", p.text).strip()
            out[matched_name] = _SectionHit(
                header=p,
                inline_content=inline,
                items=[],
            )
            current_section = matched_name
            continue

        # Otherwise, paragraph belongs to the current section's items.
        if current_section is not None:
            current_items.append(p)

    _flush()
    return out


def _build_text_field(
    document_id: str,
    section: _SectionHit | list[_Paragraph] | None,
    *,
    required: bool,
) -> ExtractedField[str] | None:
    """Build a free-text ExtractedField from a section result.

    If the section has inline content, that's the value. Otherwise
    join the items into a single newline-separated string. ``required``
    controls whether absence returns None (optional) or an abstain
    field (required-but-missing).
    """

    if isinstance(section, _SectionHit) and section.inline_content:
        return ExtractedField[str](
            value=section.inline_content,
            citation=_cite(document_id, section.header),
        )
    if isinstance(section, _SectionHit) and section.items:
        joined = "\n".join(item.text for item in section.items)
        return ExtractedField[str](
            value=joined,
            citation=_cite(document_id, section.header),
        )
    if isinstance(section, list) and section:
        joined = "\n".join(item.text for item in section)
        return ExtractedField[str](
            value=joined,
            citation=_cite(document_id, section[0]),
        )
    if required:
        return ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    return None


def _build_list(
    document_id: str,
    items_or_section: _SectionHit | list[_Paragraph],
) -> list[ExtractedField[str]]:
    """Materialize a list section as a list[ExtractedField[str]]."""

    items = (
        items_or_section.items
        if isinstance(items_or_section, _SectionHit)
        else items_or_section
    )
    return [
        ExtractedField[str](value=item.text, citation=_cite(document_id, item))
        for item in items
    ]
