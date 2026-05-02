"""Integration test — engine output against the PR 13a seeded scenarios.

Constructs a :class:`PatientChart` for each of the five seeded conflict
scenarios from :file:`tests/Tests/Fixtures/discrepancy-scenarios.php`
(reproduced in Python here to keep the agent-service test isolated from
the PHP toolchain) and asserts the engine produces the expected flags.

This is the headline acceptance for PR 13c: the same engine and the
same default rule packs that production wires up must emit one flag per
scenario, in the right category, citing the right source records. PR
13d's parity test extends this by also loading the SQL fixture path and
verifying both routes produce identical flag sets.

Determinism: the stale-lab rule reads the current date when no ``as_of``
is configured. The seeded HbA1c is dated ``2024-08-15`` so any test run
on or after ``2025-08-15`` produces the expected stale flag. We pin
``as_of`` explicitly via :func:`_freeze_stale_lab_clock` so the test is
not date-dependent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from clinical_copilot.discrepancy.engine import (
    DiscrepancyEngine,
    PatientChart,
)
from clinical_copilot.discrepancy.rules import (
    DEFAULT_PACK_PATHS,
    DEFAULT_REGISTRY,
    StaleChronicLabRule,
)
from clinical_copilot.tools.records import (
    AllergyRecord,
    FlagRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine() -> DiscrepancyEngine:
    """Build the production engine, then pin the stale-lab clock.

    Pinning ``as_of`` rather than ``freeze_time``-ing the system clock
    keeps the test scoped to the rule that actually reads it. Other
    rules read no clock today; PR 13d extends this helper if more
    rules grow date dependencies.
    """

    engine = DiscrepancyEngine.from_yaml(DEFAULT_PACK_PATHS, DEFAULT_REGISTRY)
    _freeze_stale_lab_clock(engine, as_of=date(2026, 5, 2))
    return engine


def _freeze_stale_lab_clock(engine: DiscrepancyEngine, *, as_of: date) -> None:
    for rule in engine.rules:
        if isinstance(rule, StaleChronicLabRule):
            # ``_as_of`` is a private cache the rule reads at evaluate
            # time. Tests are allowed to write it because the deterministic
            # alternative (full from_configs API + per-test YAML override)
            # is more code without more clarity.
            rule._as_of = as_of  # see docstring above


def _flags_for_rule(flags: Sequence[FlagRecord], rule_id: str) -> list[FlagRecord]:
    return [flag for flag in flags if flag.rule_id == rule_id]


# ---------------------------------------------------------------------------
# Scenario charts (mirrors discrepancy-scenarios.php)
# ---------------------------------------------------------------------------


def _scenario_med_vs_note() -> PatientChart:
    return PatientChart(
        patient_id="90001",
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/scenario1-metoprolol",
                name="Metoprolol Tartrate 50mg",
                status="active",
                started_on="2024-06-01",
            ),
        ),
        notes=(
            NoteRecord(
                source_id="DocumentReference/scenario1-note",
                note_date="2026-04-15",
                author="Dr. Test",
                body=(
                    "Patient developed bradycardia. Discontinued metoprolol "
                    "effective today; follow up in two weeks for BP recheck."
                ),
            ),
        ),
    )


def _scenario_narrative_only_allergy() -> PatientChart:
    return PatientChart(
        patient_id="90002",
        # Empty allergies — that's the gap the rule should surface.
        allergies=(),
        notes=(
            NoteRecord(
                source_id="DocumentReference/scenario2-note",
                note_date="2026-04-15",
                author="Dr. Test",
                body=(
                    "Patient reports a sulfa allergy — developed rash on "
                    "Bactrim as a teen. No allergy listed in chart prior to "
                    "this visit."
                ),
            ),
        ),
    )


def _scenario_resolved_problem_still_active() -> PatientChart:
    return PatientChart(
        patient_id="90003",
        problems=(
            ProblemRecord(
                source_id="Condition/scenario3-htn",
                code="ICD10:I10",
                display="Hypertension",
                onset_date="2024-06-01",
                status="active",
            ),
        ),
        notes=(
            NoteRecord(
                source_id="DocumentReference/scenario3-note",
                note_date="2026-04-15",
                author="Dr. Test",
                body=(
                    "BP 118/76 today, sustained over six months. Patient "
                    "has completed taper off lisinopril. Considering "
                    "hypertension resolved."
                ),
            ),
        ),
    )


def _scenario_allergen_med_safety() -> PatientChart:
    return PatientChart(
        patient_id="90004",
        allergies=(
            AllergyRecord(
                source_id="AllergyIntolerance/scenario4-pcn",
                substance="Penicillin",
                reaction="hives",
                severity="confirmed",
            ),
        ),
        medications=(
            MedicationRecord(
                source_id="MedicationRequest/scenario4-amox",
                name="Amoxicillin 500mg",
                status="active",
                started_on="2026-03-20",
            ),
        ),
    )


def _scenario_stale_chronic_lab() -> PatientChart:
    return PatientChart(
        patient_id="90005",
        problems=(
            ProblemRecord(
                source_id="Condition/scenario5-t2dm",
                code="ICD10:E11.9",
                display="Type 2 Diabetes Mellitus",
                onset_date="2022-03-15",
                status="active",
            ),
        ),
        labs=(
            LabRecord(
                source_id="Observation/scenario5-a1c",
                code="4548-4",
                display="Hemoglobin A1c",
                value="7.8",
                unit="%",
                observed_on="2024-08-15",
                reference_range="4.0-5.6",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Per-scenario assertions — the headline acceptance for PR 13c.
# ---------------------------------------------------------------------------


def test_scenario_med_vs_note_flags_consistency() -> None:
    flags = _engine().evaluate(_scenario_med_vs_note())
    matching = _flags_for_rule(flags, "med_vs_note_conflict")
    assert len(matching) == 1
    flag = matching[0]
    assert flag.category == "consistency"
    assert flag.referenced_source_ids == [
        "MedicationRequest/scenario1-metoprolol",
        "DocumentReference/scenario1-note",
    ]


def test_scenario_narrative_only_allergy_flags_consistency() -> None:
    flags = _engine().evaluate(_scenario_narrative_only_allergy())
    matching = _flags_for_rule(flags, "narrative_only_allergy")
    assert len(matching) == 1
    flag = matching[0]
    assert flag.category == "consistency"
    assert flag.referenced_source_ids == ["DocumentReference/scenario2-note"]
    assert "sulfa" in flag.rationale.lower()


def test_scenario_resolved_problem_flags_data_quality() -> None:
    flags = _engine().evaluate(_scenario_resolved_problem_still_active())
    matching = _flags_for_rule(flags, "resolved_problem_still_active")
    assert len(matching) == 1
    flag = matching[0]
    assert flag.category == "data_quality"
    assert flag.referenced_source_ids == [
        "Condition/scenario3-htn",
        "DocumentReference/scenario3-note",
    ]


def test_scenario_allergen_med_safety_flags_safety() -> None:
    flags = _engine().evaluate(_scenario_allergen_med_safety())
    matching = _flags_for_rule(flags, "allergen_med_safety_conflict")
    assert len(matching) == 1
    flag = matching[0]
    assert flag.category == "safety"
    assert flag.referenced_source_ids == [
        "AllergyIntolerance/scenario4-pcn",
        "MedicationRequest/scenario4-amox",
    ]


def test_scenario_stale_chronic_lab_flags_data_quality() -> None:
    flags = _engine().evaluate(_scenario_stale_chronic_lab())
    matching = _flags_for_rule(flags, "stale_chronic_lab")
    assert len(matching) == 1
    flag = matching[0]
    assert flag.category == "data_quality"
    assert flag.referenced_source_ids == [
        "Condition/scenario5-t2dm",
        "Observation/scenario5-a1c",
    ]


# ---------------------------------------------------------------------------
# Cross-scenario assertions — each scenario produces exactly the expected
# rule's flag, not its neighbors. Confirms we have no rule that silently
# fires across scenario boundaries.
# ---------------------------------------------------------------------------


def test_each_scenario_produces_only_its_expected_rule() -> None:
    engine = _engine()
    expected: dict[str, str] = {
        "scenario1": "med_vs_note_conflict",
        "scenario2": "narrative_only_allergy",
        "scenario3": "resolved_problem_still_active",
        "scenario4": "allergen_med_safety_conflict",
        "scenario5": "stale_chronic_lab",
    }
    charts = {
        "scenario1": _scenario_med_vs_note(),
        "scenario2": _scenario_narrative_only_allergy(),
        "scenario3": _scenario_resolved_problem_still_active(),
        "scenario4": _scenario_allergen_med_safety(),
        "scenario5": _scenario_stale_chronic_lab(),
    }
    for label, chart in charts.items():
        flags = engine.evaluate(chart)
        rule_ids = sorted({flag.rule_id for flag in flags})
        assert rule_ids == [expected[label]], (
            f"{label} should fire exactly {expected[label]!r}, got {rule_ids}"
        )


def test_expected_flag_set_across_all_scenarios() -> None:
    """Aggregate sanity check — five scenarios, five distinct rule_ids."""

    engine = _engine()
    charts = [
        _scenario_med_vs_note(),
        _scenario_narrative_only_allergy(),
        _scenario_resolved_problem_still_active(),
        _scenario_allergen_med_safety(),
        _scenario_stale_chronic_lab(),
    ]
    all_rule_ids = sorted({flag.rule_id for chart in charts for flag in engine.evaluate(chart)})
    assert all_rule_ids == sorted(
        [
            "med_vs_note_conflict",
            "narrative_only_allergy",
            "resolved_problem_still_active",
            "allergen_med_safety_conflict",
            "stale_chronic_lab",
        ],
    )
