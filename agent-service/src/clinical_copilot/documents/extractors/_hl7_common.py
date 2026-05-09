"""HL7 v2 segment-walking primitives shared by the ORU and ADT
extractors.

Both extractors split the message on ``\\r``, walk segments
positionally, and resolve coded sub-fields by ``^``. The segment
splitter, field accessor, coded-component splitter, datetime parser,
and citation helper are identical between the two — pulled out here
so a future segment-handling fix lands in one place.

This module is intentionally tiny — adding HL7-message-specific
parsing logic to it (e.g. message-type validation) would couple
the two extractors. Keep this file to format primitives only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from clinical_copilot.documents.schemas.citation import SourceCitation


@dataclass(frozen=True, slots=True)
class Segment:
    """One HL7 segment with its 1-based line number for citations.

    ``fields`` is the result of ``raw.split("|")``; for MSH this means
    ``fields[0] == "MSH"``, ``fields[1]`` is the encoding chars, and
    ``fields[2]`` is the sending app — matching the spec, off-by-one
    from a "field 1 is the first data field" reading.
    """

    name: str
    fields: list[str]
    line_number: int
    raw: str


def split_segments(raw: str) -> list[Segment]:
    """Split on ``\\r`` (HL7 standard), tolerating ``\\n``/``\\r\\n``
    that appear when an HL7 file has been opened-and-saved by a
    text editor on a non-HL7-aware machine."""

    normalized = raw.replace("\r\n", "\r").replace("\n", "\r")
    out: list[Segment] = []
    for index, line in enumerate(normalized.split("\r")):
        if not line.strip():
            continue
        fields = line.split("|")
        out.append(
            Segment(
                name=fields[0],
                fields=fields,
                line_number=index + 1,
                raw=line,
            )
        )
    return out


def find_segment(segments: list[Segment], name: str) -> Segment | None:
    for s in segments:
        if s.name == name:
            return s
    return None


def find_all_segments(segments: list[Segment], name: str) -> list[Segment]:
    return [s for s in segments if s.name == name]


def cite(document_id: str, segment: Segment, *, path: str) -> SourceCitation:
    """SourceCitation for one segment, bound to a schema-walk path.

    ``page`` overloads the citation slot to encode the 1-based segment
    number; ``raw_text`` carries the verbatim segment (capped at 240
    chars so a long OBX still fits). ``path`` is the JSON-pointer-style
    schema-walk position of the leaf this citation belongs to (e.g.
    ``"patient_name"``, ``"observations[2].value"``) and is bound onto
    the citation's ``field_or_chunk_id``.
    """

    return SourceCitation(
        document_id=document_id,
        page=segment.line_number,
        bbox=(0.0, 0.0, 1.0, 1.0),
        confidence=1.0,
        raw_text=segment.raw[:240],
        field_or_chunk_id=path,
    )


def safe_field(segment: Segment, index: int) -> str:
    """Return ``segment.fields[index]`` or empty string when out of
    range. Spares every caller a length check."""

    if index >= len(segment.fields):
        return ""
    return segment.fields[index]


def coded_components(field: str) -> tuple[str, str, str]:
    """Split a coded field (``code^display^codingSystem``) into its
    three components, returning empty strings for missing parts."""

    parts = field.split("^")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def parse_hl7_datetime(raw: str) -> date | None:
    """Parse the ``YYYYMMDD`` or ``YYYYMMDDHHMMSS`` prefix to a date.

    HL7 datetimes are truncated-as-needed: a date-only field carries 8
    digits, a date-time carries 14. Anything shorter is invalid."""

    if len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").date()
    except ValueError:
        return None
