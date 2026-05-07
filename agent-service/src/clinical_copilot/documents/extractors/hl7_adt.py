"""HL7 v2 ADT-A08 extractor (Week 2 multimodal expansion, Step 7).

Parses an "Update Patient Information" message into an
:class:`Hl7AdtFacts`. Shares the segment-walking primitives with the
ORU extractor (split on ``\\r``, tolerate ``\\n``/``\\r\\n``,
positional fields off-by-one for MSH, coded fields sub-delimited
with ``^``).

The cohort-5 ADT messages carry six segment families we model:

  * ``EVN`` — event metadata, including the human-readable reason
    for the update in EVN-7.
  * ``PID`` — patient demographics (same as ORU).
  * ``PD1`` — additional patient details, especially the PCP in
    PD1-4 (NPI^LAST^FIRST^MIDDLE).
  * ``NK1`` — next of kin / emergency contact (one or more).
  * ``IN1`` — insurance info (one per coverage).
  * ``PV1`` — visit info (not extracted; visit context is not
    part of the demographics-update review).

The downstream review surface (``document_review.php``) takes the
extracted patient demographics and asks the patient resolver for
matching charts; the clinician then confirms a match or triggers
the create-new-patient workflow.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from anthropic import Anthropic

from clinical_copilot.documents.extractors._hl7_common import (
    Segment,
    cite as hl7_cite,
    coded_components,
    find_segment,
    parse_hl7_datetime,
    safe_field,
    split_segments,
)
from clinical_copilot.documents.schemas.citation import ExtractedField
from clinical_copilot.documents.schemas.hl7_adt import Hl7AdtFacts
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def extract_hl7_adt(
    *,
    client: Anthropic,
    model: str,
    document_id: str,
    document_path: Path,
) -> Hl7AdtFacts:
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

    evn = find_segment(segments, "EVN")
    pd1 = find_segment(segments, "PD1")
    nk1 = find_segment(segments, "NK1")
    in1 = find_segment(segments, "IN1")

    pid_fields = _extract_pid(document_id, pid)
    evn_fields = _extract_evn(document_id, evn)
    pd1_fields = _extract_pd1(document_id, pd1)
    nk1_fields = _extract_nk1(document_id, nk1)
    in1_fields = _extract_in1(document_id, in1)

    return Hl7AdtFacts(
        document_id=document_id,
        patient_name=pid_fields["name"],
        patient_dob=pid_fields["dob"],
        patient_mrn=pid_fields["mrn"],
        patient_sex=pid_fields["sex"],
        patient_address=pid_fields["address"],
        patient_phone=pid_fields["phone"],
        patient_race=pid_fields["race"],
        update_reason=evn_fields["reason"],
        primary_care_provider=pd1_fields["pcp"],
        primary_care_provider_npi=pd1_fields["pcp_npi"],
        next_of_kin_name=nk1_fields["name"],
        next_of_kin_relationship=nk1_fields["relationship"],
        next_of_kin_phone=nk1_fields["phone"],
        insurance_carrier=in1_fields["carrier"],
        insurance_plan_id=in1_fields["plan_id"],
        insurance_member_id=in1_fields["member_id"],
        insurance_group_number=in1_fields["group"],
    )


# ---------------------------------------------------------------------
# Per-segment extractors
# ---------------------------------------------------------------------


def _extract_pid(document_id: str, pid: Segment) -> dict[str, ExtractedField | None]:
    """Same PID layout as ORU; ADT also carries address, phone, race."""

    cite = hl7_cite(document_id, pid)

    pid5 = safe_field(pid, 5)
    last, first, _middle = coded_components(pid5)
    name_str = " ".join(part for part in (first.strip().title(), last.strip().title()) if part)
    name_field: ExtractedField[str]
    if name_str:
        name_field = ExtractedField[str](value=name_str, citation=cite)
    else:
        name_field = ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA)

    parsed_dob = parse_hl7_datetime(safe_field(pid, 7))
    dob_field: ExtractedField[date] | None = (
        ExtractedField[date](value=parsed_dob, citation=cite)
        if parsed_dob is not None
        else None
    )

    pid3 = safe_field(pid, 3)
    mrn_value, *_ = pid3.split("^") if pid3 else ("",)
    mrn_field: ExtractedField[str] | None = (
        ExtractedField[str](value=mrn_value, citation=cite) if mrn_value else None
    )

    sex = safe_field(pid, 8).strip()
    sex_field: ExtractedField[str] | None = (
        ExtractedField[str](value=sex, citation=cite) if sex else None
    )

    # PID-11 — address (multi-component: line^line2^city^state^zip^country).
    pid11 = safe_field(pid, 11)
    address_field: ExtractedField[str] | None = None
    if pid11:
        address_parts = pid11.split("^")
        line = address_parts[0] if address_parts else ""
        city = address_parts[2] if len(address_parts) > 2 else ""
        state = address_parts[3] if len(address_parts) > 3 else ""
        zip_code = address_parts[4] if len(address_parts) > 4 else ""
        printable = ", ".join(p for p in (line, city, state, zip_code) if p)
        if printable:
            address_field = ExtractedField[str](value=printable, citation=cite)

    # PID-13 — home phone (multi-component, area code in component 5/6).
    pid13 = safe_field(pid, 13)
    phone_field: ExtractedField[str] | None = None
    if pid13:
        phone_parts = pid13.split("^")
        # Cohort-5 layout: ^PRN^PH^^^AAA^NUMBER
        area = phone_parts[5] if len(phone_parts) > 5 else ""
        number = phone_parts[6] if len(phone_parts) > 6 else ""
        if area and number:
            digits_only = "".join(c for c in number if c.isdigit())
            if len(digits_only) >= 7:
                phone_field = ExtractedField[str](
                    value=f"({area}) {digits_only[:3]}-{digits_only[3:7]}",
                    citation=cite,
                )
        elif phone_parts[0]:
            phone_field = ExtractedField[str](value=phone_parts[0], citation=cite)

    # PID-10 — race (coded). Cohort-5 uses HL70005 codes like
    # `2028-9^Asian^HL70005`; we surface the display name.
    pid10 = safe_field(pid, 10)
    race_field: ExtractedField[str] | None = None
    if pid10:
        _race_code, race_display, _race_system = coded_components(pid10)
        if race_display:
            race_field = ExtractedField[str](value=race_display, citation=cite)

    return {
        "name": name_field,
        "dob": dob_field,
        "mrn": mrn_field,
        "sex": sex_field,
        "address": address_field,
        "phone": phone_field,
        "race": race_field,
    }


def _extract_evn(document_id: str, evn: Segment | None) -> dict[str, ExtractedField | None]:
    """EVN layout: per HL7 v2.5.1 the spec assigns date/code semantics
    to EVN-3..7, but the cohort-5 data convention puts a human-readable
    "what happened" string in EVN-6 (the Event-Occurred slot, treated
    as free text for demo purposes). EVN-7 is also accepted as a
    fallback for senders that conform to spec more strictly."""

    if evn is None:
        return {"reason": None}
    cite = hl7_cite(document_id, evn)
    for idx in (6, 7):
        reason = safe_field(evn, idx).strip()
        if reason and not reason.isdigit():  # skip when a sender used the slot for an actual date
            return {"reason": ExtractedField[str](value=reason, citation=cite)}
    return {"reason": None}


def _extract_pd1(document_id: str, pd1: Segment | None) -> dict[str, ExtractedField | None]:
    """PD1-4 — primary care provider (NPI^LAST^FIRST^MIDDLE^^^^^NPI)."""

    if pd1 is None:
        return {"pcp": None, "pcp_npi": None}
    cite = hl7_cite(document_id, pd1)
    pd1_4 = safe_field(pd1, 4)
    if not pd1_4:
        return {"pcp": None, "pcp_npi": None}

    parts = pd1_4.split("^")
    npi = parts[0] if parts and parts[0] else ""
    last = parts[1].strip().title() if len(parts) > 1 else ""
    first = parts[2].strip().title() if len(parts) > 2 else ""
    name = " ".join(p for p in (first, last) if p)

    return {
        "pcp": ExtractedField[str](value=name, citation=cite) if name else None,
        "pcp_npi": ExtractedField[str](value=npi, citation=cite) if npi else None,
    }


def _extract_nk1(document_id: str, nk1: Segment | None) -> dict[str, ExtractedField | None]:
    """NK1-2 = name (LAST^FIRST), NK1-3 = relationship code, NK1-5 = phone."""

    if nk1 is None:
        return {"name": None, "relationship": None, "phone": None}
    cite = hl7_cite(document_id, nk1)

    nk1_2 = safe_field(nk1, 2)
    name_field: ExtractedField[str] | None = None
    if nk1_2:
        last, first, _ = coded_components(nk1_2)
        full = " ".join(p for p in (first.strip().title(), last.strip().title()) if p)
        if full:
            name_field = ExtractedField[str](value=full, citation=cite)

    nk1_3 = safe_field(nk1, 3).strip()
    relationship_field: ExtractedField[str] | None = (
        ExtractedField[str](value=nk1_3, citation=cite) if nk1_3 else None
    )

    nk1_5 = safe_field(nk1, 5)
    phone_field: ExtractedField[str] | None = None
    if nk1_5:
        phone_parts = nk1_5.split("^")
        area = phone_parts[5] if len(phone_parts) > 5 else ""
        number = phone_parts[6] if len(phone_parts) > 6 else ""
        if area and number:
            digits_only = "".join(c for c in number if c.isdigit())
            if len(digits_only) >= 7:
                phone_field = ExtractedField[str](
                    value=f"({area}) {digits_only[:3]}-{digits_only[3:7]}",
                    citation=cite,
                )

    return {"name": name_field, "relationship": relationship_field, "phone": phone_field}


def _extract_in1(document_id: str, in1: Segment | None) -> dict[str, ExtractedField | None]:
    """IN1 layout — the cohort-5 data convention shifts a few fields
    one slot from the strict HL7 v2.5.1 spec, so the indices below
    match what the source produces:
      - IN1-2: plan id        (BSCA001)
      - IN1-4: carrier name   (BLUE SHIELD OF CALIFORNIA PPO)
      - IN1-9: group number   (100942)        — spec puts this at IN1-8
      - IN1-35: member id     (XEH123456789)  — spec puts this at IN1-36
    """

    if in1 is None:
        return {"carrier": None, "plan_id": None, "member_id": None, "group": None}
    cite = hl7_cite(document_id, in1)

    carrier = safe_field(in1, 4).strip()
    plan_id = safe_field(in1, 2).strip()
    group = safe_field(in1, 9).strip()
    member_id = safe_field(in1, 35).strip()

    return {
        "carrier": ExtractedField[str](value=carrier, citation=cite) if carrier else None,
        "plan_id": ExtractedField[str](value=plan_id, citation=cite) if plan_id else None,
        "member_id": ExtractedField[str](value=member_id, citation=cite) if member_id else None,
        "group": ExtractedField[str](value=group, citation=cite) if group else None,
    }
