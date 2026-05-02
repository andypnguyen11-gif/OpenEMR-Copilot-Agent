"""Unit tests for the field-level value check (ARCHITECTURE §3 layer 4).

The field check is claim-type-aware: structured-fact, temporal, and
categorical claims have different comparators. Each test below exercises
exactly one comparator via :func:`find_field_mismatches` so a failure
points at the right one. Middleware-level integration through
:class:`VerificationMiddleware` lives in ``test_verification.py``.

Field kinds for the test fixtures:

* ``LabRecord.value`` / ``LabRecord.display`` — STRUCTURED_FACT
* ``LabRecord.observed_on`` / ``ProblemRecord.onset_date`` — TEMPORAL
* ``ProblemRecord.status`` / ``VisitRecord.encounter_type`` — CATEGORICAL
"""

from __future__ import annotations

import pytest

from clinical_copilot.orchestrator.schemas import CitedClaim
from clinical_copilot.tools.records import (
    LabRecord,
    ProblemRecord,
    ToolResult,
    VisitRecord,
)
from clinical_copilot.verification import field_check as fc
from clinical_copilot.verification.field_check import (
    FieldCheckError,
    FieldKind,
    find_field_mismatches,
    resolve_field_kind,
)


def _wrap(record: LabRecord | ProblemRecord | VisitRecord) -> ToolResult:
    return ToolResult(tool_name="test", patient_id="101", records=[record])


def _lab() -> LabRecord:
    return LabRecord(
        source_id="Observation/lab1",
        code="4548-4",
        display="Hemoglobin A1c",
        value="7.1",
        unit="%",
        observed_on="2026-03-14",
    )


def _problem(*, status: str = "active", onset: str | None = "2019-04-12") -> ProblemRecord:
    return ProblemRecord(
        source_id="Condition/c1",
        code="44054006",
        display="Type 2 diabetes mellitus",
        onset_date=onset,
        status=status,
    )


def _visit(*, encounter_type: str = "Office visit") -> VisitRecord:
    return VisitRecord(
        source_id="Encounter/e1",
        encounter_type=encounter_type,
        visited_on="2026-04-01",
    )


# ---------------------------------------------------------------------------
# resolve_field_kind — the dispatch table is part of the contract
# ---------------------------------------------------------------------------


def test_resolve_kind_classifies_known_temporal_field() -> None:
    assert resolve_field_kind(LabRecord, "observed_on") is FieldKind.TEMPORAL


def test_resolve_kind_classifies_known_categorical_field() -> None:
    assert resolve_field_kind(ProblemRecord, "status") is FieldKind.CATEGORICAL


def test_resolve_kind_defaults_unmapped_field_to_structured_fact() -> None:
    assert resolve_field_kind(LabRecord, "value") is FieldKind.STRUCTURED_FACT


# ---------------------------------------------------------------------------
# Structured-fact claims: exact equality after trim+casefold
# ---------------------------------------------------------------------------


def test_structured_fact_exact_match_passes() -> None:
    claim = CitedClaim(
        text="A1c is 7.1.",
        source_id="Observation/lab1",
        source_field="value",
        expected_value="7.1",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())]) == []


def test_structured_fact_value_mismatch_returns_mismatch() -> None:
    claim = CitedClaim(
        text="A1c is 9.4.",
        source_id="Observation/lab1",
        source_field="value",
        expected_value="9.4",
    )
    mismatches = find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())])
    assert len(mismatches) == 1
    assert mismatches[0].source_field == "value"
    assert mismatches[0].expected == "9.4"
    assert mismatches[0].actual == "7.1"


def test_structured_fact_passes_after_trim_and_casefold() -> None:
    claim = CitedClaim(
        text="A1c label.",
        source_id="Observation/lab1",
        source_field="display",
        expected_value="  hemoglobin a1c  ",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())]) == []


# ---------------------------------------------------------------------------
# Temporal claims: ISO-date parse + tolerance window
# ---------------------------------------------------------------------------


def test_temporal_exact_match_passes() -> None:
    claim = CitedClaim(
        text="Lab observed on 2026-03-14.",
        source_id="Observation/lab1",
        source_field="observed_on",
        expected_value="2026-03-14",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())]) == []


def test_temporal_off_by_one_day_passes_within_default_tolerance() -> None:
    """One-day default tolerance absorbs timezone/phrasing edges
    (e.g., 'yesterday' parsed against UTC midnight on the boundary)."""

    claim = CitedClaim(
        text="Lab observed yesterday.",
        source_id="Observation/lab1",
        source_field="observed_on",
        expected_value="2026-03-15",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())]) == []


def test_temporal_outside_tolerance_returns_mismatch() -> None:
    claim = CitedClaim(
        text="Lab observed weeks ago.",
        source_id="Observation/lab1",
        source_field="observed_on",
        expected_value="2026-03-25",
    )
    mismatches = find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())])
    assert len(mismatches) == 1
    assert mismatches[0].source_field == "observed_on"
    assert mismatches[0].expected == "2026-03-25"
    assert mismatches[0].actual == "2026-03-14"


def test_temporal_unparsable_expected_returns_mismatch() -> None:
    """Free-form temporal phrasing the comparator can't parse fails
    conservatively. The model is supposed to emit an ISO date; a fuzzy
    string can hide what it actually claimed."""

    claim = CitedClaim(
        text="Lab observed last week.",
        source_id="Observation/lab1",
        source_field="observed_on",
        expected_value="last week",
    )
    mismatches = find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())])
    assert len(mismatches) == 1


def test_temporal_actual_none_returns_mismatch() -> None:
    """ProblemRecord.onset_date is optional. A claim asserting a value
    against a None record field is a fabricated assertion — fail."""

    claim = CitedClaim(
        text="Onset 2019-04-12.",
        source_id="Condition/c1",
        source_field="onset_date",
        expected_value="2019-04-12",
    )
    mismatches = find_field_mismatches(
        claims=[claim],
        tool_results=[_wrap(_problem(onset=None))],
    )
    assert len(mismatches) == 1
    assert mismatches[0].actual is None


# ---------------------------------------------------------------------------
# Categorical claims: enum membership + value match
# ---------------------------------------------------------------------------


def test_categorical_match_inside_vocab_passes() -> None:
    claim = CitedClaim(
        text="Active.",
        source_id="Condition/c1",
        source_field="status",
        expected_value="active",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_problem())]) == []


def test_categorical_wrong_value_returns_mismatch() -> None:
    claim = CitedClaim(
        text="Resolved.",
        source_id="Condition/c1",
        source_field="status",
        expected_value="resolved",
    )
    mismatches = find_field_mismatches(claims=[claim], tool_results=[_wrap(_problem())])
    assert len(mismatches) == 1
    assert mismatches[0].source_field == "status"


def test_categorical_invented_value_returns_mismatch_even_when_strings_match() -> None:
    """Out-of-vocab value should fail the categorical check regardless of
    whether it happens to match the actual on the record. Catches the
    case where the model invents a status word and a fixture quirk
    happens to hold the same fabricated string."""

    claim = CitedClaim(
        text="Visit type.",
        source_id="Encounter/e1",
        source_field="encounter_type",
        expected_value="Mystery type",
    )
    mismatches = find_field_mismatches(
        claims=[claim],
        tool_results=[_wrap(_visit(encounter_type="Mystery type"))],
    )
    assert len(mismatches) == 1


def test_categorical_handles_capitalized_fixture_values() -> None:
    """The visit fixture stores ``encounter_type='Office visit'`` with a
    capital O. Vocab is case-folded so a lowercase claim still passes."""

    claim = CitedClaim(
        text="Office visit.",
        source_id="Encounter/e1",
        source_field="encounter_type",
        expected_value="office visit",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_visit())]) == []


# ---------------------------------------------------------------------------
# Skipped / errored cases
# ---------------------------------------------------------------------------


def test_existence_only_claim_is_skipped() -> None:
    """A claim with neither source_field nor expected_value is the
    citation-existence-only path. Field-check skips it."""

    claim = CitedClaim(text="The patient has T2DM.", source_id="Condition/c1")
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_problem())]) == []


def test_unresolved_source_id_is_skipped_for_field_check() -> None:
    """Citation-check owns fabricated source_ids; field-check skips them
    so the same offense isn't double-counted in the abstention reason."""

    claim = CitedClaim(
        text="Imaginary condition.",
        source_id="Condition/never-fetched",
        source_field="display",
        expected_value="anything",
    )
    assert find_field_mismatches(claims=[claim], tool_results=[_wrap(_problem())]) == []


def test_unknown_field_name_raises_field_check_error() -> None:
    """A claim that names a field the record's class doesn't have is the
    model's mistake (not a data-quality issue) — surface explicitly so
    the middleware can map it to VERIFICATION_FAILED with a distinct
    reason."""

    claim = CitedClaim(
        text="Mood is great.",
        source_id="Observation/lab1",
        source_field="mood",
        expected_value="great",
    )
    with pytest.raises(FieldCheckError):
        find_field_mismatches(claims=[claim], tool_results=[_wrap(_lab())])


def test_categorical_field_with_no_vocab_raises_field_check_error() -> None:
    """A field declared CATEGORICAL but missing from the vocab map is a
    programming error — fail loudly so it surfaces in test, not in prod."""

    # Patch ``_CATEGORICAL_VOCAB`` so a CATEGORICAL field has no vocab
    # entry — the assertion is that the check raises rather than
    # silently passing any value through.
    saved = fc._CATEGORICAL_VOCAB.get(ProblemRecord, {}).get("status")
    try:
        # Drop status from the vocab map for this test.
        fc._CATEGORICAL_VOCAB[ProblemRecord] = {}
        claim = CitedClaim(
            text="Active.",
            source_id="Condition/c1",
            source_field="status",
            expected_value="active",
        )
        with pytest.raises(FieldCheckError):
            find_field_mismatches(claims=[claim], tool_results=[_wrap(_problem())])
    finally:
        if saved is not None:
            fc._CATEGORICAL_VOCAB[ProblemRecord] = {"status": saved}
