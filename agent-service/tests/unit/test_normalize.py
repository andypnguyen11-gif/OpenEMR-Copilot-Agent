"""Unit tests for the discrepancy engine's normalization helpers.

Drug-name normalization is load-bearing: AUDIT D-02 lists it as
table-stakes, and the rule logic in PR 13b/c collapses against
:func:`normalize_drug_name` and :func:`primary_drug_token` only — drift
here turns into silent false negatives in the engine's flags.
"""

from __future__ import annotations

import pytest

from clinical_copilot.discrepancy.normalize import (
    normalize_code,
    normalize_drug_name,
    primary_drug_token,
    text_contains,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Metoprolol Tartrate 50mg", "metoprolol tartrate"),
        ("Amoxicillin 500 MG", "amoxicillin"),
        ("Lisinopril 10 mg", "lisinopril"),
        ("  Lisinopril   ", "lisinopril"),
        ("Levothyroxine 50 mcg", "levothyroxine"),
        ("Vitamin D3 1000 IU", "vitamin d3"),
        ("Amoxicillin (oral suspension), 500mg", "amoxicillin oral suspension"),
        ("", ""),
    ],
)
def test_normalize_drug_name_strips_dose_and_lowercases(raw: str, expected: str) -> None:
    assert normalize_drug_name(raw) == expected


def test_normalize_drug_name_does_not_mangle_embedded_digits() -> None:
    # "B12" is part of the name, not a dose specifier (no whitespace +
    # digit + unit). Keep it intact.
    assert normalize_drug_name("Vitamin B12") == "vitamin b12"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Metoprolol Tartrate 50mg", "metoprolol"),
        ("Amoxicillin 500mg", "amoxicillin"),
        ("Vitamin D3 1000 IU", "vitamin"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_primary_drug_token_returns_leading_generic(raw: str, expected: str) -> None:
    assert primary_drug_token(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("RXCUI:866924", "rxcui:866924"),
        ("rxcui:866924", "rxcui:866924"),
        ("ICD10:E11.9", "icd10:e11.9"),
        ("snomed:44054006", "snomed:44054006"),
        ("LOINC:4548-4", "loinc:4548-4"),
        (" 866924 ", "866924"),
        ("866924", "866924"),
    ],
)
def test_normalize_code_canonicalizes_known_systems(raw: str, expected: str) -> None:
    assert normalize_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_normalize_code_returns_none_on_blank(raw: str) -> None:
    assert normalize_code(raw) is None


def test_normalize_code_unknown_prefix_falls_back_to_lower() -> None:
    # Unknown prefix is preserved verbatim (lowercased) so callers can
    # still compare two raw codes from the same unknown source.
    assert normalize_code("FOO:bar") == "foo:bar"


def test_text_contains_is_case_insensitive_and_collapses_whitespace() -> None:
    assert text_contains("Patient is taking METOPROLOL today", "metoprolol")
    assert text_contains("Patient\nis\ttaking metoprolol", "metoprolol")
    assert not text_contains("Patient is taking lisinopril", "metoprolol")


def test_text_contains_empty_needle_is_false() -> None:
    # An empty needle would be vacuously contained in every string;
    # callers benefit from the guarded answer.
    assert not text_contains("anything", "")
