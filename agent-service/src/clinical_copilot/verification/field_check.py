"""Field-level value check.

Layer 4 of ARCHITECTURE §3. When a :class:`CitedClaim` includes a
``source_field`` and an ``expected_value``, the field check resolves the
record by ``source_id`` and compares the value the model claims to the
value the record actually carries. A mismatch fails the response — no
"close enough" path.

The comparison is conservative string equality after a small
normalization (trim + casefold). This is correct for the M-PR fixture
(structured-fact and categorical claims dominate); PR 11 extends with
temporal-tolerance and enum-membership variants.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from clinical_copilot.orchestrator.schemas import CitedClaim
from clinical_copilot.tools.records import AnyRecord, ToolResult


@dataclass(frozen=True, slots=True)
class FieldMismatch:
    """One offending claim. Aggregating these in the middleware lets us
    emit a single response-level abstention with all the offending
    citations rather than failing on the first."""

    source_id: str
    source_field: str
    expected: str
    actual: str | None


class FieldCheckError(Exception):
    """Raised when a claim references an unknown field on a known
    record. Surfaces from :func:`check_field` to the middleware, which
    treats it as a per-claim verification failure (the field name is
    the model's mistake, not a data-quality issue)."""


def index_records(tool_results: list[ToolResult]) -> dict[str, AnyRecord]:
    """Return a lookup from ``source_id`` to the fetched record.

    Duplicates (same source_id from two tools) are tolerated — later
    wins. This shouldn't happen with the fixture but a future FHIR
    bundle merge could legitimately produce one.
    """

    by_id: dict[str, AnyRecord] = {}
    for result in tool_results:
        for record in result.records:
            by_id[record.source_id] = record
    return by_id


def find_field_mismatches(
    *,
    claims: list[CitedClaim],
    tool_results: list[ToolResult],
) -> list[FieldMismatch]:
    """Return field-level disagreements between the draft and the
    fetched records. Empty list means every checked claim's structural
    field matches.

    Claims without ``source_field`` and ``expected_value`` are skipped —
    they are existence-only citations and only the citation-check layer
    speaks to them.
    """

    by_id = index_records(tool_results)
    mismatches: list[FieldMismatch] = []
    for claim in claims:
        if claim.source_field is None or claim.expected_value is None:
            continue
        record = by_id.get(claim.source_id)
        if record is None:
            # Unresolved citation — handled by the citation_check layer;
            # skip here so we don't double-count the same offense.
            continue
        actual = _read_field(record, claim.source_field)
        if not _equivalent(actual, claim.expected_value):
            mismatches.append(
                FieldMismatch(
                    source_id=claim.source_id,
                    source_field=claim.source_field,
                    expected=claim.expected_value,
                    actual=actual,
                )
            )
    return mismatches


def _read_field(record: BaseModel, field_name: str) -> str | None:
    """Pydantic models don't allow arbitrary attribute access for fields
    that aren't declared, so we look up via ``model_fields`` first and
    fall back to ``getattr`` only for declared fields. An undeclared
    field name is the model's invention — :class:`FieldCheckError`
    surfaces upward.
    """

    if field_name not in type(record).model_fields:
        raise FieldCheckError(
            f"claim references unknown field {field_name!r} on {type(record).__name__}"
        )
    value = getattr(record, field_name)
    if value is None:
        return None
    return str(value)


def _equivalent(actual: str | None, expected: str) -> bool:
    """String comparison after trim + casefold.

    Conservative on purpose: anything beyond this (numeric tolerance,
    date windows) is layered into PR 11. For M2 we want false-negatives
    to be visible failures, not silent passes.
    """

    if actual is None:
        return False
    return actual.strip().casefold() == expected.strip().casefold()
