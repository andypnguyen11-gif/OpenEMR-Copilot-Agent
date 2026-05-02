"""Unit tests for the discrepancy engine ABC + YAML loader + the seed rule.

Pins the engine contract that PR 13c/d depend on:

* The :class:`PatientChart` shape and its tuple-of-records inputs.
* The YAML loader's behavior on enabled, disabled, and unknown rule ids.
* Deterministic ``flag_source_id`` so eval expectations stay reproducible.
* :class:`MedVsNoteConflictRule` flagging exactly when the active medication
  appears in a recent note alongside a configured conflict keyword, and
  emitting nothing in the symmetric negative cases.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from clinical_copilot.discrepancy.engine import (
    DiscrepancyEngine,
    DiscrepancyEngineError,
    DiscrepancyRule,
    PatientChart,
    RuleConfig,
    RuleConfigMismatchError,
    UnknownRuleError,
    flag_source_id,
)
from clinical_copilot.discrepancy.rules import (
    CONSISTENCY_PACK,
    DEFAULT_REGISTRY,
    MedVsNoteConflictRule,
)
from clinical_copilot.tools.records import (
    FlagRecord,
    MedicationRecord,
    NoteRecord,
)

# ---------------------------------------------------------------------------
# Test data factories — keeps each test's intent obvious by listing only the
# fields the test cares about.
# ---------------------------------------------------------------------------


def _med(
    *,
    source_id: str = "MedicationRequest/1",
    name: str = "Metoprolol Tartrate 50mg",
    status: str = "active",
) -> MedicationRecord:
    return MedicationRecord(source_id=source_id, name=name, status=status, started_on="2024-06-01")


def _note(
    *,
    source_id: str = "DocumentReference/1",
    body: str = "Patient is doing well on current regimen.",
    note_date: str = "2026-04-15",
) -> NoteRecord:
    return NoteRecord(
        source_id=source_id,
        note_date=note_date,
        author="Dr. Test",
        body=body,
    )


def _chart(
    *,
    medications: Sequence[MedicationRecord] = (),
    notes: Sequence[NoteRecord] = (),
) -> PatientChart:
    return PatientChart(
        patient_id="90001",
        medications=tuple(medications),
        notes=tuple(notes),
    )


def _config(
    *,
    rule_id: str = "med_vs_note_conflict",
    category: str = "consistency",
    enabled: bool = True,
    params: dict[str, object] | None = None,
) -> RuleConfig:
    return RuleConfig(
        id=rule_id,
        category=category,
        description="test config",
        enabled=enabled,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# PatientChart + flag_source_id contract
# ---------------------------------------------------------------------------


def test_patient_chart_is_frozen() -> None:
    chart = _chart()
    with pytest.raises(ValidationError):
        chart.patient_id = "different"


def test_flag_source_id_is_deterministic_for_same_inputs() -> None:
    a = flag_source_id(
        rule_id="med_vs_note_conflict",
        patient_id="90001",
        referenced_source_ids=["MedicationRequest/1", "DocumentReference/1"],
    )
    b = flag_source_id(
        rule_id="med_vs_note_conflict",
        patient_id="90001",
        # Order is normalized via sort, so swapping inputs cannot drift the id.
        referenced_source_ids=["DocumentReference/1", "MedicationRequest/1"],
    )
    assert a == b
    assert a.startswith("flag/med_vs_note_conflict/")


def test_flag_source_id_differs_for_different_referenced_records() -> None:
    a = flag_source_id(
        rule_id="r",
        patient_id="p",
        referenced_source_ids=["X/1"],
    )
    b = flag_source_id(
        rule_id="r",
        patient_id="p",
        referenced_source_ids=["X/2"],
    )
    assert a != b


# ---------------------------------------------------------------------------
# DiscrepancyRule ABC contract
# ---------------------------------------------------------------------------


class _FakeRule(DiscrepancyRule):
    rule_id: ClassVar[str] = "fake"
    category: ClassVar[str] = "consistency"

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        return []


def test_rule_class_id_must_match_config_id() -> None:
    with pytest.raises(RuleConfigMismatchError):
        _FakeRule(_config(rule_id="something_else"))


def test_rule_class_category_must_match_config_category() -> None:
    with pytest.raises(RuleConfigMismatchError):
        _FakeRule(_config(rule_id="fake", category="safety"))


def test_rule_construction_accepts_matching_config() -> None:
    rule = _FakeRule(_config(rule_id="fake", category="consistency"))
    assert rule.rule_id == "fake"
    assert rule.description == "test config"


# ---------------------------------------------------------------------------
# DiscrepancyEngine.from_yaml — loader behavior
# ---------------------------------------------------------------------------


def test_engine_loads_consistency_pack_with_default_registry() -> None:
    engine = DiscrepancyEngine.from_yaml([CONSISTENCY_PACK], DEFAULT_REGISTRY)
    rule_classes = {type(rule) for rule in engine.rules}
    # PR 13c added narrative_only_allergy alongside med_vs_note_conflict;
    # both must instantiate cleanly from the consistency pack.
    assert MedVsNoteConflictRule in rule_classes
    assert all(rule.category == "consistency" for rule in engine.rules)


def test_engine_skips_disabled_rules(tmp_path: Path) -> None:
    pack = tmp_path / "pack.yaml"
    pack.write_text(
        """\
rules:
  - id: med_vs_note_conflict
    category: consistency
    enabled: false
""",
        encoding="utf-8",
    )
    engine = DiscrepancyEngine.from_yaml([pack], DEFAULT_REGISTRY)
    assert engine.rules == ()


def test_engine_raises_on_unknown_rule_id(tmp_path: Path) -> None:
    pack = tmp_path / "pack.yaml"
    pack.write_text(
        """\
rules:
  - id: not_a_real_rule
    category: consistency
""",
        encoding="utf-8",
    )
    with pytest.raises(UnknownRuleError) as exc_info:
        DiscrepancyEngine.from_yaml([pack], DEFAULT_REGISTRY)
    assert exc_info.value.rule_id == "not_a_real_rule"
    assert exc_info.value.source == pack


def test_engine_raises_on_malformed_yaml(tmp_path: Path) -> None:
    pack = tmp_path / "pack.yaml"
    pack.write_text("rules: not-a-list\n", encoding="utf-8")
    with pytest.raises(DiscrepancyEngineError):
        DiscrepancyEngine.from_yaml([pack], DEFAULT_REGISTRY)


def test_engine_raises_on_missing_pack(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(DiscrepancyEngineError):
        DiscrepancyEngine.from_yaml([missing], DEFAULT_REGISTRY)


# ---------------------------------------------------------------------------
# MedVsNoteConflictRule — happy path + symmetric negatives
# ---------------------------------------------------------------------------


def _med_vs_note_rule(**param_overrides: object) -> MedVsNoteConflictRule:
    return MedVsNoteConflictRule(_config(params=dict(param_overrides) or {}))


def test_med_vs_note_flags_active_med_with_recent_discontinuation_note() -> None:
    rule = _med_vs_note_rule()
    chart = _chart(
        medications=[_med()],
        notes=[
            _note(
                body="Patient developed bradycardia. Discontinued metoprolol effective today.",
                note_date="2026-04-15",
            ),
        ],
    )
    flags = list(rule.evaluate(chart))
    assert len(flags) == 1
    flag = flags[0]
    assert flag.rule_id == "med_vs_note_conflict"
    assert flag.category == "consistency"
    assert flag.referenced_source_ids == ["MedicationRequest/1", "DocumentReference/1"]
    assert "metoprolol" in flag.rationale.lower()


def test_med_vs_note_does_not_flag_when_med_is_not_active() -> None:
    rule = _med_vs_note_rule()
    chart = _chart(
        medications=[_med(status="stopped")],
        notes=[_note(body="Discontinued metoprolol.")],
    )
    assert list(rule.evaluate(chart)) == []


def test_med_vs_note_does_not_flag_when_note_does_not_mention_drug() -> None:
    rule = _med_vs_note_rule()
    chart = _chart(
        medications=[_med()],
        notes=[_note(body="Discontinued lisinopril; switching to ARB.")],
    )
    assert list(rule.evaluate(chart)) == []


def test_med_vs_note_does_not_flag_when_no_conflict_keyword() -> None:
    rule = _med_vs_note_rule()
    chart = _chart(
        medications=[_med()],
        notes=[_note(body="Continue metoprolol at current dose; BP stable.")],
    )
    assert list(rule.evaluate(chart)) == []


def test_med_vs_note_respects_look_back_notes_window() -> None:
    rule = _med_vs_note_rule(look_back_notes=1)
    chart = _chart(
        medications=[_med()],
        notes=[
            # Most recent note has nothing concerning.
            _note(
                source_id="DocumentReference/recent",
                body="Continue current regimen.",
                note_date="2026-04-15",
            ),
            # Older note discontinues the drug, but should be outside the window.
            _note(
                source_id="DocumentReference/older",
                body="Discontinued metoprolol.",
                note_date="2024-01-15",
            ),
        ],
    )
    assert list(rule.evaluate(chart)) == []


def test_med_vs_note_emits_one_flag_per_medication_even_with_multiple_notes() -> None:
    rule = _med_vs_note_rule()
    chart = _chart(
        medications=[_med()],
        notes=[
            _note(
                source_id="DocumentReference/a",
                body="Discontinued metoprolol — bradycardia.",
                note_date="2026-04-15",
            ),
            _note(
                source_id="DocumentReference/b",
                body="Stopped metoprolol last visit; confirming today.",
                note_date="2026-04-10",
            ),
        ],
    )
    flags = list(rule.evaluate(chart))
    assert len(flags) == 1
    # The most-recent note is the one referenced (the rule matches the
    # first qualifying note in date-desc order).
    assert flags[0].referenced_source_ids == [
        "MedicationRequest/1",
        "DocumentReference/a",
    ]


def test_med_vs_note_rejects_invalid_look_back_notes() -> None:
    with pytest.raises(ValueError):
        MedVsNoteConflictRule(
            _config(params={"look_back_notes": 0}),
        )
    with pytest.raises(ValueError):
        MedVsNoteConflictRule(
            _config(params={"look_back_notes": "three"}),
        )


def test_med_vs_note_rejects_non_list_keywords() -> None:
    with pytest.raises(ValueError):
        MedVsNoteConflictRule(
            _config(params={"conflict_keywords": "discontinued"}),
        )


def test_med_vs_note_rejects_empty_keyword_list() -> None:
    with pytest.raises(ValueError):
        MedVsNoteConflictRule(
            _config(params={"conflict_keywords": []}),
        )


# ---------------------------------------------------------------------------
# DiscrepancyEngine.evaluate — end-to-end load + run
# ---------------------------------------------------------------------------


def test_engine_evaluate_runs_loaded_rules_against_chart() -> None:
    engine = DiscrepancyEngine.from_yaml([CONSISTENCY_PACK], DEFAULT_REGISTRY)
    chart = _chart(
        medications=[_med()],
        notes=[
            _note(
                body="Patient developed bradycardia. Discontinued metoprolol effective today.",
                note_date="2026-04-15",
            ),
        ],
    )
    flags = engine.evaluate(chart)
    assert len(flags) == 1
    assert flags[0].rule_id == "med_vs_note_conflict"


def test_engine_evaluate_returns_empty_for_empty_chart() -> None:
    engine = DiscrepancyEngine.from_yaml([CONSISTENCY_PACK], DEFAULT_REGISTRY)
    chart = PatientChart(patient_id="ghost")
    assert engine.evaluate(chart) == []
