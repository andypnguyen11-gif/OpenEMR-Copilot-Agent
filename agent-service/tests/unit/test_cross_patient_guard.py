"""Unit tests for the deterministic cross-patient guard.

Covers the two extraction patterns and the comparator semantics.
Drives :mod:`cross_patient_guard` directly so failures point at the
guard, not the orchestrator wiring.
"""

from __future__ import annotations

import pytest

from clinical_copilot.orchestrator.cross_patient_guard import (
    cross_patient_check,
    extract_referenced_names,
)


# ---- extract_referenced_names ---------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("what are patient Queenie medications", ["Queenie"]),
        ("show me Patient Maria's labs", ["Maria"]),
        ("compare patient Andrew and patient Beth", ["Andrew", "Beth"]),
        ("Queenie's allergies?", ["Queenie"]),
        ("any of Maria's recent labs", ["Maria"]),
        # Possessive with curly apostrophe still matches.
        ("show Marcus’s problems", ["Marcus"]),
    ],
    ids=[
        "patient-prefix-bare",
        "patient-prefix-possessive",
        "patient-prefix-multiple",
        "possessive-bare",
        "possessive-with-leading-text",
        "possessive-curly-apostrophe",
    ],
)
def test_extracts_explicit_name_patterns(query: str, expected: list[str]) -> None:
    assert extract_referenced_names(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        # No name attached to ``patient`` — bare reference.
        "what should I know about this patient before walking in?",
        # Drug names are Title-Case but lack an explicit syntactic anchor.
        "is the patient on metoprolol",
        "Tylenol dose for adults",
        # Sentence-initial verbs / pronouns are filtered by the 3-char + Title-Case rule for ``X's``.
        "He's been on lisinopril",
        # Lowercase tokens after ``patient`` are not names.
        "the patient who came in yesterday",
    ],
    ids=[
        "bare-patient",
        "drug-name-not-name",
        "drug-name-sentence-initial",
        "pronoun-possessive",
        "lowercase-after-patient",
    ],
)
def test_skips_non_name_patterns(query: str) -> None:
    assert extract_referenced_names(query) == []


def test_dedupes_case_insensitively_preserving_first_seen() -> None:
    # ``Maria`` appears three times in two distinct positions; output is one entry.
    assert extract_referenced_names(
        "Maria's labs, then patient Maria's meds, then MARIA's allergies",
    ) == ["Maria"]


# ---- cross_patient_check --------------------------------------------------


@pytest.mark.parametrize(
    ("query", "bound", "expected_substring"),
    [
        (
            "what are patient Queenie medications",
            "Ping Collins",
            "Queenie",
        ),
        (
            "Maria's labs",
            "Daniel Brooks",
            "Maria",
        ),
        (
            "compare patient Andrew and patient Beth",
            "Sofia Chen",
            "Andrew",
        ),
    ],
    ids=["patient-prefix", "possessive", "multiple-mismatches"],
)
def test_mismatch_returns_reason(query: str, bound: str, expected_substring: str) -> None:
    reason = cross_patient_check(query, bound)
    assert reason is not None
    assert expected_substring in reason
    assert bound in reason


@pytest.mark.parametrize(
    ("query", "bound"),
    [
        # Exact full name match.
        ("Maria's labs", "Maria Lopez"),
        # First-name match — query has just first, bound has first + last.
        ("Marcus's medications", "Marcus Hayes"),
        # Last-name match — query has just last, bound has full.
        ("patient Hayes is overdue", "Marcus Hayes"),
    ],
    ids=["full-match", "first-name-match", "last-name-match"],
)
def test_match_passes_through(query: str, bound: str) -> None:
    assert cross_patient_check(query, bound) is None


def test_query_without_a_name_passes_through() -> None:
    assert cross_patient_check("what active problems does this patient have?", "Marcus Hayes") is None


def test_missing_bound_name_passes_through() -> None:
    # When the resolver could not look up the patient name, the guard is
    # disabled (the prompt-side rule remains the only line of defense).
    assert cross_patient_check("Queenie's labs", None) is None
    assert cross_patient_check("Queenie's labs", "") is None


def test_short_or_lowercase_bound_parts_are_ignored_in_substring_match() -> None:
    # ``Lopez`` is the only ≥3-char part of ``"De Lopez"`` after the
    # length filter; a query asking about ``Sara`` should still flag.
    assert cross_patient_check("Sara's chart", "De Lopez") is not None
