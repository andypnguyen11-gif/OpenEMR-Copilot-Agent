"""HL7 v2 ORU-R01 extractor (Week 2 multimodal expansion, Step 6).

Parses the structured pipe-delimited HL7 stream into an
:class:`Hl7OruFacts` model. The downstream confirm path is the same
``lab_review.php`` page used for lab-PDF observations because both
extractors emit ``LabObservation`` rows — the only HL7-specific bit
is the patient-demographics block lifted from the PID segment, which
the document-review page uses for patient resolution.

HL7 v2 quirks worth knowing for this extractor:

  * Segments are separated by carriage return (``\\r``), not newline.
  * Field separator is ``|``; field 1 of MSH is the encoding chars
    (``^~\\&``) which means MSH is parsed slightly differently from
    every other segment (its fields are off-by-one from a naive split).
  * Coded fields (e.g. LOINC in OBX-3) are sub-delimited with ``^``;
    the standard layout is ``code^display^codingSystem``.
  * Datetime fields (MSH-7, OBX-14, etc.) use the ``YYYYMMDDHHMMSS``
    truncated-as-needed format, never ISO 8601.

The cohort-5 corpus uses one ORU per file with one OBR per ORU and
4-5 OBX rows. The extractor is tolerant of multi-OBR cases (every
OBX is collected regardless of which OBR it follows) but only reports
the first OBR's metadata in ``order_panel`` / ``order_loinc`` /
``specimen_collected_at`` / ``ordering_provider``.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from anthropic import Anthropic

from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.hl7_oru import Hl7OruFacts
from clinical_copilot.documents.schemas.lab_pdf import LabObservation
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def extract_hl7_oru(
    *,
    client: Anthropic,
    model: str,
    document_id: str,
    document_path: Path,
) -> Hl7OruFacts:
    """Public registry entry point. Signature matches the other
    extractors so the registry dispatch is uniform."""

    del client, model  # text-only extractor

    raw = document_path.read_text(encoding="utf-8", errors="replace")
    segments = _split_segments(raw)
    if not segments:
        raise ValueError(f"HL7 file is empty / no segments: {document_path}")

    pid = _find_segment(segments, "PID")
    if pid is None:
        raise ValueError(f"HL7 file has no PID segment: {document_path}")

    obr = _find_segment(segments, "OBR")  # first OBR; ORC is also common but optional
    obx_rows = _find_all_segments(segments, "OBX")

    patient_fields = _extract_pid(document_id, pid)
    order_fields = _extract_obr(document_id, obr) if obr is not None else _empty_order_fields()
    observations = [
        _extract_obx(document_id, obx) for obx in obx_rows
    ]

    return Hl7OruFacts(
        document_id=document_id,
        patient_name=patient_fields["name"],
        patient_dob=patient_fields["dob"],
        patient_mrn=patient_fields["mrn"],
        patient_sex=patient_fields["sex"],
        order_panel=order_fields["panel"],
        order_loinc=order_fields["loinc"],
        specimen_collected_at=order_fields["collected_at"],
        ordering_provider=order_fields["provider"],
        observations=observations,
    )


# ---------------------------------------------------------------------
# Segment-level helpers
# ---------------------------------------------------------------------


class _Segment:
    """One HL7 segment with its 1-based line number for citations."""

    __slots__ = ("name", "fields", "line_number", "raw")

    def __init__(self, name: str, fields: list[str], line_number: int, raw: str) -> None:
        self.name = name
        self.fields = fields
        self.line_number = line_number
        self.raw = raw


def _split_segments(raw: str) -> list[_Segment]:
    """Split on \\r (HL7 standard) but tolerate \\n / \\r\\n line endings
    that appear when an HL7 file has been opened-and-saved by a
    text editor on a non-HL7-aware machine.
    """

    # Normalize all line endings to \r so the split is uniform.
    normalized = raw.replace("\r\n", "\r").replace("\n", "\r")
    out: list[_Segment] = []
    for index, line in enumerate(normalized.split("\r")):
        if not line.strip():
            continue
        fields = line.split("|")
        # MSH is special: field 1 is the encoding chars (^~\&), not a
        # data field, so positional indices in MSH are off-by-one from
        # every other segment. We fix that by inserting a synthetic
        # field-0 marker (the segment name) so callers can index
        # fields[N] consistently across segments.
        if fields[0] == "MSH":
            # MSH already has field 0 = "MSH", field 1 = "^~\&", field
            # 2 = sending app, ... matching the spec; do not transform.
            pass
        out.append(_Segment(
            name=fields[0],
            fields=fields,
            line_number=index + 1,
            raw=line,
        ))
    return out


def _find_segment(segments: list[_Segment], name: str) -> _Segment | None:
    for s in segments:
        if s.name == name:
            return s
    return None


def _find_all_segments(segments: list[_Segment], name: str) -> list[_Segment]:
    return [s for s in segments if s.name == name]


def _cite(document_id: str, segment: _Segment) -> SourceCitation:
    return SourceCitation(
        document_id=document_id,
        page=segment.line_number,
        bbox=(0.0, 0.0, 1.0, 1.0),
        confidence=1.0,
        raw_text=segment.raw[:240],  # cap so very long OBX rows fit
    )


def _safe_field(segment: _Segment, index: int) -> str:
    """Return field at index or empty string when out of range."""

    if index >= len(segment.fields):
        return ""
    return segment.fields[index]


def _coded_components(field: str) -> tuple[str, str, str]:
    """Split a coded field (`code^display^codingSystem`) into its
    components, returning empty strings for missing parts."""

    parts = field.split("^")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _parse_hl7_datetime(raw: str) -> date | None:
    """Parse YYYYMMDD or YYYYMMDDHHMMSS prefix to a date."""

    if len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------
# Per-segment extractors
# ---------------------------------------------------------------------


def _extract_pid(document_id: str, pid: _Segment) -> dict[str, ExtractedField | None]:
    """PID layout (HL7 v2.5.1):
      - PID-3: patient identifier list (`MRN^^^^assigner^MR`)
      - PID-5: patient name (`LAST^FIRST^MIDDLE`)
      - PID-7: date of birth (YYYYMMDD)
      - PID-8: administrative sex
    """

    cite = _cite(document_id, pid)

    # PID-5 — patient name.
    pid5 = _safe_field(pid, 5)
    last, first, _middle = _coded_components(pid5)
    name_str = " ".join(part for part in (first.strip().title(), last.strip().title()) if part)
    name_field: ExtractedField[str]
    if name_str:
        name_field = ExtractedField[str](value=name_str, citation=cite)
    else:
        name_field = ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)

    # PID-7 — DOB.
    dob_raw = _safe_field(pid, 7)
    parsed_dob = _parse_hl7_datetime(dob_raw)
    dob_field: ExtractedField[date] | None = None
    if parsed_dob is not None:
        dob_field = ExtractedField[date](value=parsed_dob, citation=cite)

    # PID-3 — MRN. The cohort-5 layout puts the MRN in component 0,
    # followed by `^^^MRN^MR` to mark it as a medical record number.
    pid3 = _safe_field(pid, 3)
    mrn_value, *_ = pid3.split("^") if pid3 else ("",)
    mrn_field: ExtractedField[str] | None = None
    if mrn_value:
        mrn_field = ExtractedField[str](value=mrn_value, citation=cite)

    # PID-8 — sex.
    sex = _safe_field(pid, 8).strip()
    sex_field: ExtractedField[str] | None = None
    if sex:
        sex_field = ExtractedField[str](value=sex, citation=cite)

    return {"name": name_field, "dob": dob_field, "mrn": mrn_field, "sex": sex_field}


def _empty_order_fields() -> dict[str, ExtractedField | None]:
    return {"panel": None, "loinc": None, "collected_at": None, "provider": None}


def _extract_obr(document_id: str, obr: _Segment) -> dict[str, ExtractedField | None]:
    """OBR layout (HL7 v2.5.1):
      - OBR-4: universal service identifier (`code^display^codingSystem`)
      - OBR-7: observation date/time (YYYYMMDDHHMMSS)
      - OBR-16: ordering provider (`NPI^LAST^FIRST^MIDDLE^^^^^NPI`)
    """

    cite = _cite(document_id, obr)
    out: dict[str, ExtractedField | None] = _empty_order_fields()

    obr4 = _safe_field(obr, 4)
    code, display, coding_system = _coded_components(obr4)
    if display:
        out["panel"] = ExtractedField[str](value=display, citation=cite)
    if code and coding_system.upper() == "LN":
        out["loinc"] = ExtractedField[str](value=code, citation=cite)

    obr7 = _safe_field(obr, 7)
    parsed = _parse_hl7_datetime(obr7)
    if parsed is not None:
        out["collected_at"] = ExtractedField[date](value=parsed, citation=cite)

    obr16 = _safe_field(obr, 16)
    if obr16:
        # NPI^LAST^FIRST^MIDDLE^^^^^NPI — pull last + first.
        parts = obr16.split("^")
        if len(parts) >= 3:
            last_p = parts[1].strip().title()
            first_p = parts[2].strip().title()
            display_name = " ".join(p for p in (first_p, last_p) if p)
            if display_name:
                out["provider"] = ExtractedField[str](value=display_name, citation=cite)

    return out


def _extract_obx(document_id: str, obx: _Segment) -> LabObservation:
    """OBX layout (HL7 v2.5.1):
      - OBX-3: observation identifier (`code^display^codingSystem`)
      - OBX-5: observation value (numeric for NM type)
      - OBX-6: units
      - OBX-7: reference range (text — may be `<200`, `>=40`, `40-100`, etc.)
      - OBX-8: abnormal flags
      - OBX-14: observation date/time
    """

    cite = _cite(document_id, obx)

    code, display, _coding_system = _coded_components(_safe_field(obx, 3))
    code_str = code or display
    code_field = (
        ExtractedField[str](value=code_str, citation=cite)
        if code_str
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )
    display_field = (
        ExtractedField[str](value=display, citation=cite)
        if display
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )

    raw_value = _safe_field(obx, 5).strip()
    value_field: ExtractedField[float]
    try:
        value_field = ExtractedField[float](value=float(raw_value), citation=cite)
    except (TypeError, ValueError):
        # Non-numeric OBX values (textual results, etc.) get an abstain.
        value_field = ExtractedField[float](abstain_reason=RuntimeAbstainReason.OUT_OF_SCHEMA)

    unit = _safe_field(obx, 6).strip()
    unit_field = (
        ExtractedField[str](value=unit, citation=cite)
        if unit
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )

    obx14 = _safe_field(obx, 14)
    parsed_dt = _parse_hl7_datetime(obx14)
    effective_field: ExtractedField[date]
    if parsed_dt is not None:
        effective_field = ExtractedField[date](value=parsed_dt, citation=cite)
    else:
        effective_field = ExtractedField[date](abstain_reason=RuntimeAbstainReason.NO_DATA)

    # Reference range OBX-7 is a free-text string in HL7, not a low/high
    # tuple. We don't try to parse it into reference_low / reference_high
    # (which is doable but error-prone — '<200' has no low bound, etc.).
    # Leave reference_low / reference_high as None and surface the raw
    # range string via the citation raw_text. The lab_review page can
    # render the raw segment if needed.

    flag = _safe_field(obx, 8).strip()
    flag_field: ExtractedField[str] | None = None
    if flag:
        flag_field = ExtractedField[str](value=flag, citation=cite)

    return LabObservation(
        code=code_field,
        display=display_field,
        value=value_field,
        unit=unit_field,
        effective_date=effective_field,
        reference_low=None,
        reference_high=None,
        flag=flag_field,
    )
