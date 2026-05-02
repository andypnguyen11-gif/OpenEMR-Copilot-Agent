"""Per-rule unit tests for the four PR 13c discrepancy rules.

The seeded-fixture integration test pins the happy paths against the
five canonical scenarios; this file pins the negative branches that
matter most for false-positive control.
"""

from __future__ import annotations

from datetime import date

import pytest

from clinical_copilot.discrepancy.engine import (
    PatientChart,
    RuleConfig,
)
from clinical_copilot.discrepancy.rules.allergen_med_safety_conflict import (
    AllergenMedSafetyConflictRule,
)
from clinical_copilot.discrepancy.rules.lab_out_of_range import LabOutOfRangeRule
from clinical_copilot.discrepancy.rules.narrative_only_allergy import (
    NarrativeOnlyAllergyRule,
)
from clinical_copilot.discrepancy.rules.resolved_problem_still_active import (
    ResolvedProblemStillActiveRule,
)
from clinical_copilot.discrepancy.rules.stale_chronic_lab import StaleChronicLabRule
from clinical_copilot.tools.records import (
    AllergyRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
)


def _config(rule_id: str, category: str, **params: object) -> RuleConfig:
    return RuleConfig(id=rule_id, category=category, params=params)


# ---------------------------------------------------------------------------
# narrative_only_allergy
# ---------------------------------------------------------------------------


def _narr_rule() -> NarrativeOnlyAllergyRule:
    return NarrativeOnlyAllergyRule(_config("narrative_only_allergy", "consistency"))


def _narr_chart(*, body: str, allergies: tuple[AllergyRecord, ...] = ()) -> PatientChart:
    return PatientChart(
        patient_id="P",
        notes=(
            NoteRecord(
                source_id="DocumentReference/n",
                note_date="2026-04-15",
                author="Dr. Test",
                body=body,
            ),
        ),
        allergies=allergies,
    )


def test_narrative_only_allergy_does_not_flag_when_allergy_already_listed() -> None:
    chart = _narr_chart(
        body="Patient reports a sulfa allergy.",
        allergies=(
            AllergyRecord(
                source_id="AllergyIntolerance/sulfa",
                substance="Sulfa",
                reaction="rash",
                severity="confirmed",
            ),
        ),
    )
    assert list(_narr_rule().evaluate(chart)) == []


def test_narrative_only_allergy_does_not_flag_without_marker_word() -> None:
    # Bare 'sulfa' with no allergy/allergic marker — could be a med name
    # or an unrelated mention. Don't flag.
    chart = _narr_chart(body="Switching to a non-sulfa antibiotic.")
    assert list(_narr_rule().evaluate(chart)) == []


def test_narrative_only_allergy_does_not_flag_without_keyword() -> None:
    chart = _narr_chart(body="Patient denies any allergies on review of systems.")
    assert list(_narr_rule().evaluate(chart)) == []


def test_narrative_only_allergy_emits_one_flag_per_keyword() -> None:
    chart = _narr_chart(body="Allergic to peanuts and latex; no other known allergies.")
    flags = list(_narr_rule().evaluate(chart))
    keywords = sorted({kw for kw in ("peanut", "latex") for flag in flags if kw in flag.rationale})
    assert keywords == ["latex", "peanut"]


# ---------------------------------------------------------------------------
# resolved_problem_still_active
# ---------------------------------------------------------------------------


def _resolved_rule() -> ResolvedProblemStillActiveRule:
    return ResolvedProblemStillActiveRule(_config("resolved_problem_still_active", "data_quality"))


def test_resolved_problem_does_not_flag_inactive_problem() -> None:
    chart = PatientChart(
        patient_id="P",
        problems=(
            ProblemRecord(
                source_id="Condition/c1",
                code="ICD10:I10",
                display="Hypertension",
                onset_date="2024-01-01",
                status="resolved",
            ),
        ),
        notes=(
            NoteRecord(
                source_id="DocumentReference/n1",
                note_date="2026-04-15",
                author="Dr. Test",
                body="Hypertension resolved.",
            ),
        ),
    )
    assert list(_resolved_rule().evaluate(chart)) == []


def test_resolved_problem_does_not_flag_when_note_does_not_name_problem() -> None:
    chart = PatientChart(
        patient_id="P",
        problems=(
            ProblemRecord(
                source_id="Condition/c1",
                code="ICD10:I10",
                display="Hypertension",
                onset_date="2024-01-01",
                status="active",
            ),
        ),
        notes=(
            NoteRecord(
                source_id="DocumentReference/n1",
                note_date="2026-04-15",
                author="Dr. Test",
                body="Diabetes is in remission.",
            ),
        ),
    )
    assert list(_resolved_rule().evaluate(chart)) == []


# ---------------------------------------------------------------------------
# allergen_med_safety_conflict
# ---------------------------------------------------------------------------


def _safety_rule() -> AllergenMedSafetyConflictRule:
    return AllergenMedSafetyConflictRule(_config("allergen_med_safety_conflict", "safety"))


def test_safety_does_not_flag_without_allergies() -> None:
    chart = PatientChart(
        patient_id="P",
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/m1",
                name="Amoxicillin 500mg",
                status="active",
                started_on="2026-03-20",
            ),
        ),
    )
    assert list(_safety_rule().evaluate(chart)) == []


def test_safety_does_not_flag_when_med_inactive() -> None:
    chart = PatientChart(
        patient_id="P",
        allergies=(
            AllergyRecord(
                source_id="AllergyIntolerance/a1",
                substance="Penicillin",
                reaction="hives",
                severity="confirmed",
            ),
        ),
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/m1",
                name="Amoxicillin 500mg",
                status="discontinued",
                started_on="2024-06-01",
            ),
        ),
    )
    assert list(_safety_rule().evaluate(chart)) == []


def test_safety_does_not_flag_unrelated_med_and_allergy() -> None:
    chart = PatientChart(
        patient_id="P",
        allergies=(
            AllergyRecord(
                source_id="AllergyIntolerance/a1",
                substance="Penicillin",
                reaction="hives",
                severity="confirmed",
            ),
        ),
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/m1",
                name="Lisinopril 10mg",
                status="active",
                started_on="2024-06-01",
            ),
        ),
    )
    assert list(_safety_rule().evaluate(chart)) == []


def test_safety_flags_direct_match_even_without_cross_reactivity_table() -> None:
    """Penicillin allergy + active Penicillin med — direct lookup hit."""
    chart = PatientChart(
        patient_id="P",
        allergies=(
            AllergyRecord(
                source_id="AllergyIntolerance/a1",
                substance="Penicillin",
                reaction="hives",
                severity="confirmed",
            ),
        ),
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/m1",
                name="Penicillin VK 500mg",
                status="active",
                started_on="2026-04-01",
            ),
        ),
    )
    flags = list(_safety_rule().evaluate(chart))
    assert len(flags) == 1
    assert flags[0].referenced_source_ids == [
        "AllergyIntolerance/a1",
        "MedicationRequest/m1",
    ]


# ---------------------------------------------------------------------------
# stale_chronic_lab
# ---------------------------------------------------------------------------


def _stale_rule(*, as_of: str = "2026-05-02") -> StaleChronicLabRule:
    return StaleChronicLabRule(_config("stale_chronic_lab", "data_quality", as_of=as_of))


def test_stale_lab_does_not_flag_recent_lab() -> None:
    rule = _stale_rule()
    chart = PatientChart(
        patient_id="P",
        problems=(
            ProblemRecord(
                source_id="Condition/c1",
                code="ICD10:E11.9",
                display="Type 2 Diabetes Mellitus",
                onset_date="2022-03-15",
                status="active",
            ),
        ),
        labs=(
            LabRecord(
                source_id="Observation/l1",
                code="4548-4",
                display="Hemoglobin A1c",
                value="6.9",
                unit="%",
                observed_on="2026-02-01",
                reference_range="4.0-5.6",
            ),
        ),
    )
    assert list(rule.evaluate(chart)) == []


def test_stale_lab_does_not_flag_when_problem_unrelated() -> None:
    rule = _stale_rule()
    chart = PatientChart(
        patient_id="P",
        problems=(
            ProblemRecord(
                source_id="Condition/c1",
                code="ICD10:I10",
                display="Hypertension",
                onset_date="2022-03-15",
                status="active",
            ),
        ),
        labs=(),
    )
    assert list(rule.evaluate(chart)) == []


def test_stale_lab_flags_when_no_matching_lab_at_all() -> None:
    rule = _stale_rule()
    chart = PatientChart(
        patient_id="P",
        problems=(
            ProblemRecord(
                source_id="Condition/c1",
                code="ICD10:E11.9",
                display="Type 2 Diabetes Mellitus",
                onset_date="2022-03-15",
                status="active",
            ),
        ),
        labs=(),
    )
    flags = list(rule.evaluate(chart))
    assert len(flags) == 1
    flag = flags[0]
    assert flag.referenced_source_ids == ["Condition/c1"]
    assert "no matching lab" in flag.rationale.lower()


def test_stale_lab_rejects_invalid_as_of_type() -> None:
    with pytest.raises(ValueError):
        StaleChronicLabRule(
            RuleConfig(
                id="stale_chronic_lab",
                category="data_quality",
                params={"as_of": 12345},
            ),
        )


def test_stale_lab_accepts_date_object_as_of() -> None:
    rule = StaleChronicLabRule(
        RuleConfig(
            id="stale_chronic_lab",
            category="data_quality",
            params={"as_of": date(2026, 5, 2)},
        ),
    )
    # No exception, no flag on an empty chart — sanity check only.
    assert list(rule.evaluate(PatientChart(patient_id="P"))) == []


# ---------------------------------------------------------------------------
# lab_out_of_plausible_range
# ---------------------------------------------------------------------------


def _value_sanity_rule() -> LabOutOfRangeRule:
    return LabOutOfRangeRule(_config("lab_out_of_plausible_range", "value_sanity"))


def test_value_sanity_does_not_flag_high_severity() -> None:
    """Seeded HbA1c is 'high', not panic — must not trip the value-sanity rule."""

    rule = _value_sanity_rule()
    chart = PatientChart(
        patient_id="P",
        labs=(
            LabRecord(
                source_id="Observation/l1",
                code="4548-4",
                display="Hemoglobin A1c",
                value="7.8",
                unit="%",
                observed_on="2024-08-15",
                reference_range="4.0-5.6",
            ),
        ),
    )
    assert list(rule.evaluate(chart)) == []
