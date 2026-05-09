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

from clinical_copilot.documents.extractors._hl7_common import (
    Segment,
    cite as hl7_cite,
    coded_components,
    find_all_segments,
    find_segment,
    parse_hl7_datetime,
    safe_field,
    split_segments,
)
from clinical_copilot.documents.schemas.citation import ExtractedField
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
    segments = split_segments(raw)
    if not segments:
        raise ValueError(f"HL7 file is empty / no segments: {document_path}")

    pid = find_segment(segments, "PID")
    if pid is None:
        raise ValueError(f"HL7 file has no PID segment: {document_path}")

    obr = find_segment(segments, "OBR")  # first OBR; ORC is also common but optional
    obx_rows = find_all_segments(segments, "OBX")

    patient_fields = _extract_pid(document_id, pid)
    order_fields = _extract_obr(document_id, obr) if obr is not None else _empty_order_fields()
    observations = [
        _extract_obx(document_id, obx, index=index)
        for index, obx in enumerate(obx_rows)
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
# Per-segment extractors (segment-walking primitives live in
# :mod:`clinical_copilot.documents.extractors._hl7_common`).
# ---------------------------------------------------------------------


def _extract_pid(document_id: str, pid: Segment) -> dict[str, ExtractedField | None]:
    """PID layout (HL7 v2.5.1):
      - PID-3: patient identifier list (`MRN^^^^assigner^MR`)
      - PID-5: patient name (`LAST^FIRST^MIDDLE`)
      - PID-7: date of birth (YYYYMMDD)
      - PID-8: administrative sex
    """

    # PID-5 — patient name.
    pid5 = safe_field(pid, 5)
    last, first, _middle = coded_components(pid5)
    name_str = " ".join(part for part in (first.strip().title(), last.strip().title()) if part)
    name_field: ExtractedField[str]
    if name_str:
        name_field = ExtractedField[str](
            value=name_str,
            citation=hl7_cite(document_id, pid, path="patient_name"),
        )
    else:
        name_field = ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)

    # PID-7 — DOB.
    dob_raw = safe_field(pid, 7)
    parsed_dob = parse_hl7_datetime(dob_raw)
    dob_field: ExtractedField[date] | None = None
    if parsed_dob is not None:
        dob_field = ExtractedField[date](
            value=parsed_dob,
            citation=hl7_cite(document_id, pid, path="patient_dob"),
        )

    # PID-3 — MRN. The cohort-5 layout puts the MRN in component 0,
    # followed by `^^^MRN^MR` to mark it as a medical record number.
    pid3 = safe_field(pid, 3)
    mrn_value, *_ = pid3.split("^") if pid3 else ("",)
    mrn_field: ExtractedField[str] | None = None
    if mrn_value:
        mrn_field = ExtractedField[str](
            value=mrn_value,
            citation=hl7_cite(document_id, pid, path="patient_mrn"),
        )

    # PID-8 — sex.
    sex = safe_field(pid, 8).strip()
    sex_field: ExtractedField[str] | None = None
    if sex:
        sex_field = ExtractedField[str](
            value=sex,
            citation=hl7_cite(document_id, pid, path="patient_sex"),
        )

    return {"name": name_field, "dob": dob_field, "mrn": mrn_field, "sex": sex_field}


def _empty_order_fields() -> dict[str, ExtractedField | None]:
    return {"panel": None, "loinc": None, "collected_at": None, "provider": None}


def _extract_obr(document_id: str, obr: Segment) -> dict[str, ExtractedField | None]:
    """OBR layout (HL7 v2.5.1):
      - OBR-4: universal service identifier (`code^display^codingSystem`)
      - OBR-7: observation date/time (YYYYMMDDHHMMSS)
      - OBR-16: ordering provider (`NPI^LAST^FIRST^MIDDLE^^^^^NPI`)
    """

    out: dict[str, ExtractedField | None] = _empty_order_fields()

    obr4 = safe_field(obr, 4)
    code, display, coding_system = coded_components(obr4)
    if display:
        out["panel"] = ExtractedField[str](
            value=display,
            citation=hl7_cite(document_id, obr, path="order_panel"),
        )
    if code and coding_system.upper() == "LN":
        out["loinc"] = ExtractedField[str](
            value=code,
            citation=hl7_cite(document_id, obr, path="order_loinc"),
        )

    obr7 = safe_field(obr, 7)
    parsed = parse_hl7_datetime(obr7)
    if parsed is not None:
        out["collected_at"] = ExtractedField[date](
            value=parsed,
            citation=hl7_cite(document_id, obr, path="specimen_collected_at"),
        )

    obr16 = safe_field(obr, 16)
    if obr16:
        # NPI^LAST^FIRST^MIDDLE^^^^^NPI — pull last + first.
        parts = obr16.split("^")
        if len(parts) >= 3:
            last_p = parts[1].strip().title()
            first_p = parts[2].strip().title()
            display_name = " ".join(p for p in (first_p, last_p) if p)
            if display_name:
                out["provider"] = ExtractedField[str](
                    value=display_name,
                    citation=hl7_cite(document_id, obr, path="ordering_provider"),
                )

    return out


def _extract_obx(document_id: str, obx: Segment, *, index: int) -> LabObservation:
    """OBX layout (HL7 v2.5.1):
      - OBX-3: observation identifier (`code^display^codingSystem`)
      - OBX-5: observation value (numeric for NM type)
      - OBX-6: units
      - OBX-7: reference range (text — may be `<200`, `>=40`, `40-100`, etc.)
      - OBX-8: abnormal flags
      - OBX-14: observation date/time

    ``index`` is the 0-based position in ``Hl7OruFacts.observations`` and
    is woven into each leaf citation's ``field_or_chunk_id`` (e.g.
    ``observations[2].value``).
    """

    prefix = f"observations[{index}]"

    code, display, _coding_system = coded_components(safe_field(obx, 3))
    code_str = code or display
    code_field = (
        ExtractedField[str](
            value=code_str,
            citation=hl7_cite(document_id, obx, path=f"{prefix}.code"),
        )
        if code_str
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )
    display_field = (
        ExtractedField[str](
            value=display,
            citation=hl7_cite(document_id, obx, path=f"{prefix}.display"),
        )
        if display
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )

    raw_value = safe_field(obx, 5).strip()
    value_field: ExtractedField[float]
    try:
        value_field = ExtractedField[float](
            value=float(raw_value),
            citation=hl7_cite(document_id, obx, path=f"{prefix}.value"),
        )
    except (TypeError, ValueError):
        # Non-numeric OBX values (textual results, etc.) get an abstain.
        value_field = ExtractedField[float](abstain_reason=RuntimeAbstainReason.OUT_OF_SCHEMA)

    unit = safe_field(obx, 6).strip()
    unit_field = (
        ExtractedField[str](
            value=unit,
            citation=hl7_cite(document_id, obx, path=f"{prefix}.unit"),
        )
        if unit
        else ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)
    )

    obx14 = safe_field(obx, 14)
    parsed_dt = parse_hl7_datetime(obx14)
    effective_field: ExtractedField[date]
    if parsed_dt is not None:
        effective_field = ExtractedField[date](
            value=parsed_dt,
            citation=hl7_cite(document_id, obx, path=f"{prefix}.effective_date"),
        )
    else:
        effective_field = ExtractedField[date](abstain_reason=RuntimeAbstainReason.NO_DATA)

    # Reference range OBX-7 is a free-text string in HL7, not a low/high
    # tuple. We don't try to parse it into reference_low / reference_high
    # (which is doable but error-prone — '<200' has no low bound, etc.).
    # Leave reference_low / reference_high as None and surface the raw
    # range string via the citation raw_text. The lab_review page can
    # render the raw segment if needed.

    flag = safe_field(obx, 8).strip()
    flag_field: ExtractedField[str] | None = None
    if flag:
        flag_field = ExtractedField[str](
            value=flag,
            citation=hl7_cite(document_id, obx, path=f"{prefix}.flag"),
        )

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
