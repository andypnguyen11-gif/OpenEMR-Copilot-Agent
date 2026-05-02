"""Field-level value check.

Layer 4 of ARCHITECTURE §3. When a :class:`CitedClaim` includes a
``source_field`` and an ``expected_value``, the field check resolves the
record by ``source_id`` and compares the value the model claims to the
value the record actually carries. A mismatch fails the response — no
"close enough" path.

The comparator is **claim-type-aware**, dispatched per (record class,
field name) by :func:`resolve_field_kind`:

* :attr:`FieldKind.STRUCTURED_FACT` — the default. Trim + casefold
  equality. Lab values, codes, free-text labels.
* :attr:`FieldKind.TEMPORAL` — ISO-date parse on both sides, then a
  small tolerance window (default ``_DEFAULT_TEMPORAL_TOLERANCE_DAYS``)
  to absorb "yesterday"-style off-by-one phrasing. Unparsable expected
  values fail conservatively — a free-form string can hide what was
  actually claimed.
* :attr:`FieldKind.CATEGORICAL` — match against the actual *and*
  membership in the field's enum. The membership check catches the
  failure mode where the model invents an enum value that happens to
  appear nowhere we can disprove it from a single record.

Mismatches are accumulated and returned; the middleware folds them into
one ``VERIFICATION_FAILED`` abstention. There is no "infer support from
partial match" path — that's explicitly rejected by ARCHITECTURE §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from clinical_copilot.orchestrator.schemas import CitedClaim
from clinical_copilot.tools.records import (
    AllergyRecord,
    AnyRecord,
    FlagRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
    ToolResult,
    VisitRecord,
)


class FieldKind(StrEnum):
    """How the comparator should interpret a (record, field) pair.

    Backed enum (string) because mismatch-reason serialization carries
    the kind into LangSmith spans for triage. Adding a new kind is a
    backwards-compatible change; renaming an existing one is not.
    """

    STRUCTURED_FACT = "structured_fact"
    TEMPORAL = "temporal"
    CATEGORICAL = "categorical"


_DEFAULT_TEMPORAL_TOLERANCE_DAYS = 1
"""Default temporal-comparator window. One day absorbs timezone/phrasing
edges (e.g., a UTC-midnight boundary turning "today" into a date one off
when a clinician says "yesterday"). Wider tolerances would erode the
trust story; narrower would trip on common phrasings."""


# Per-record-class field-kind table. Fields not listed default to
# STRUCTURED_FACT. Adding a new record class without a registry entry is
# safe — every field falls through to the structured-fact comparator.
_FIELD_KINDS: dict[type[BaseModel], dict[str, FieldKind]] = {
    ProblemRecord: {
        "onset_date": FieldKind.TEMPORAL,
        "status": FieldKind.CATEGORICAL,
    },
    MedicationRecord: {
        "started_on": FieldKind.TEMPORAL,
        "status": FieldKind.CATEGORICAL,
    },
    AllergyRecord: {
        "severity": FieldKind.CATEGORICAL,
    },
    LabRecord: {
        "observed_on": FieldKind.TEMPORAL,
    },
    VisitRecord: {
        "visited_on": FieldKind.TEMPORAL,
        "encounter_type": FieldKind.CATEGORICAL,
    },
    NoteRecord: {
        "note_date": FieldKind.TEMPORAL,
    },
    FlagRecord: {
        "category": FieldKind.CATEGORICAL,
    },
}


# Per-record-class allowed-value vocab for CATEGORICAL fields. Vocab
# entries are case-folded — comparators casefold inputs before checking.
# Coverage strategy: FHIR vocabularies for ``status``/``severity``;
# fixture-observed values plus near synonyms for project-specific fields
# (encounter_type, FlagRecord.category). PR 13's rules engine will
# canonicalize FlagRecord.category and this entry will narrow accordingly.
_CATEGORICAL_VOCAB: dict[type[BaseModel], dict[str, frozenset[str]]] = {
    ProblemRecord: {
        # FHIR Condition.clinicalStatus
        "status": frozenset(
            {"active", "recurrence", "relapse", "inactive", "remission", "resolved"},
        ),
    },
    MedicationRecord: {
        # FHIR MedicationRequest.status, subset relevant to a med-list
        # projection (entered-in-error / unknown stay out — they're data
        # bookkeeping rather than clinically meaningful states).
        "status": frozenset(
            {"active", "completed", "stopped", "on-hold", "cancelled", "draft"},
        ),
    },
    AllergyRecord: {
        # FHIR AllergyIntolerance.reaction.severity
        "severity": frozenset({"mild", "moderate", "severe"}),
    },
    VisitRecord: {
        "encounter_type": frozenset(
            {
                "office visit",
                "urgent care",
                "telemed",
                "telehealth",
                "follow-up",
                "annual",
                "ed",
                "emergency",
                "inpatient",
                "outpatient",
                "home",
            },
        ),
    },
    FlagRecord: {
        # Fixture uses the underscore form; the hyphen variants cover
        # what PR 13's rules engine is likely to canonicalize on.
        "category": frozenset(
            {
                "interaction",
                "value-sanity",
                "data-quality",
                "data_quality",
                "consistency",
                "safety",
                "staleness",
            },
        ),
    },
}


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
    record, or names a field declared CATEGORICAL with no vocabulary
    wired in. Both are programming/model errors (not data-quality
    issues); the middleware maps them to ``VERIFICATION_FAILED`` with a
    distinct reason."""


def resolve_field_kind(record_type: type[BaseModel], field_name: str) -> FieldKind:
    """Return the comparator kind for ``(record_type, field_name)``.

    Public so tests can pin the dispatch table without scraping internal
    attributes. Fields not listed in ``_FIELD_KINDS`` default to
    :attr:`FieldKind.STRUCTURED_FACT` — this is the safe default because
    structured-fact is the strictest comparator and adding a new record
    field never silently weakens the check.
    """

    return _FIELD_KINDS.get(record_type, {}).get(field_name, FieldKind.STRUCTURED_FACT)


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
    field matches under the comparator chosen by
    :func:`resolve_field_kind`.

    Claims without ``source_field`` and ``expected_value`` are skipped —
    they are existence-only citations and only the citation-check layer
    speaks to them. Claims pointing at unfetched records are also
    skipped here so the same offense isn't double-counted; the citation
    check owns those.

    Raises :class:`FieldCheckError` immediately on a programming error
    (unknown field on a known record, or CATEGORICAL field with no
    wired vocab). The middleware turns either into a distinct
    ``VERIFICATION_FAILED`` reason.
    """

    by_id = index_records(tool_results)
    mismatches: list[FieldMismatch] = []
    for claim in claims:
        if claim.source_field is None or claim.expected_value is None:
            continue
        record = by_id.get(claim.source_id)
        if record is None:
            # Citation_check layer owns this — skip so we don't
            # double-count the same offense.
            continue
        actual = _read_field(record, claim.source_field)
        kind = resolve_field_kind(type(record), claim.source_field)
        if not _matches(
            kind,
            actual=actual,
            expected=claim.expected_value,
            record_type=type(record),
            field_name=claim.source_field,
        ):
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


def _matches(
    kind: FieldKind,
    *,
    actual: str | None,
    expected: str,
    record_type: type[BaseModel],
    field_name: str,
) -> bool:
    """Dispatch to the right comparator. Exhaustive ``match`` (no
    ``default``) — PHPStan's Python equivalent (mypy) flags any missed
    case if a new :class:`FieldKind` is added without updating this."""

    match kind:
        case FieldKind.STRUCTURED_FACT:
            return _structured_fact_equivalent(actual, expected)
        case FieldKind.TEMPORAL:
            return _temporal_within_tolerance(actual, expected)
        case FieldKind.CATEGORICAL:
            return _categorical_in_vocab(
                actual=actual,
                expected=expected,
                record_type=record_type,
                field_name=field_name,
            )


def _structured_fact_equivalent(actual: str | None, expected: str) -> bool:
    """Trim + casefold equality. Conservative on ``None`` actuals — a
    claim asserting a value against a record field that doesn't carry
    one is a fabricated assertion."""

    if actual is None:
        return False
    return actual.strip().casefold() == expected.strip().casefold()


def _temporal_within_tolerance(actual: str | None, expected: str) -> bool:
    """Parse both sides as ISO calendar dates; pass if within
    ``_DEFAULT_TEMPORAL_TOLERANCE_DAYS``. Either side failing to parse
    fails the check — a free-form temporal string can hide what the
    model actually claimed."""

    if actual is None:
        return False
    try:
        actual_date = date.fromisoformat(actual.strip())
        expected_date = date.fromisoformat(expected.strip())
    except ValueError:
        return False
    delta_days = abs((actual_date - expected_date).days)
    return delta_days <= _DEFAULT_TEMPORAL_TOLERANCE_DAYS


def _categorical_in_vocab(
    *,
    actual: str | None,
    expected: str,
    record_type: type[BaseModel],
    field_name: str,
) -> bool:
    """Pass iff (a) ``expected`` casefold-equals ``actual``, and (b)
    ``expected`` is a member of the field's enum vocabulary. Both
    conditions matter: (a) catches "model said the wrong status,"
    (b) catches "model invented a status word."

    A categorical field with no vocab entry is a programming error —
    raises :class:`FieldCheckError` rather than silently passing any
    value through. Forcing the registry to stay in sync.
    """

    field_vocab = _CATEGORICAL_VOCAB.get(record_type, {}).get(field_name)
    if field_vocab is None:
        raise FieldCheckError(
            f"categorical field {field_name!r} on {record_type.__name__} has no vocab "
            "registered in field_check._CATEGORICAL_VOCAB",
        )
    if actual is None:
        return False
    if actual.strip().casefold() != expected.strip().casefold():
        return False
    return expected.strip().casefold() in field_vocab
