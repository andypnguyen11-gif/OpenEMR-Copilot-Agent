"""Rule pack registry — maps YAML rule ``id`` strings to implementing classes.

PR 13b shipped only ``med_vs_note_conflict`` to exercise the loader
path. PR 13c (this revision) fills out the four rule categories so the
engine emits the full expected flag set against the PR 13a seeded
fixture:

* ``consistency`` — ``med_vs_note_conflict``, ``narrative_only_allergy``
* ``data_quality`` — ``resolved_problem_still_active``, ``stale_chronic_lab``
* ``safety`` — ``allergen_med_safety_conflict``
* ``value_sanity`` — ``lab_out_of_plausible_range`` (narrow placeholder;
  the seeded HbA1c is high but plausible, so this rule does not trip
  the integration test)
"""

from __future__ import annotations

from pathlib import Path

from clinical_copilot.discrepancy.engine import DiscrepancyRule, RuleRegistry
from clinical_copilot.discrepancy.rules.allergen_med_safety_conflict import (
    AllergenMedSafetyConflictRule,
)
from clinical_copilot.discrepancy.rules.lab_out_of_range import LabOutOfRangeRule
from clinical_copilot.discrepancy.rules.med_vs_note import MedVsNoteConflictRule
from clinical_copilot.discrepancy.rules.narrative_only_allergy import (
    NarrativeOnlyAllergyRule,
)
from clinical_copilot.discrepancy.rules.resolved_problem_still_active import (
    ResolvedProblemStillActiveRule,
)
from clinical_copilot.discrepancy.rules.stale_chronic_lab import StaleChronicLabRule

_RULE_CLASSES: tuple[type[DiscrepancyRule], ...] = (
    MedVsNoteConflictRule,
    NarrativeOnlyAllergyRule,
    ResolvedProblemStillActiveRule,
    StaleChronicLabRule,
    AllergenMedSafetyConflictRule,
    LabOutOfRangeRule,
)


def _build_registry(classes: tuple[type[DiscrepancyRule], ...]) -> RuleRegistry:
    registry: dict[str, type[DiscrepancyRule]] = {}
    for cls in classes:
        if cls.rule_id in registry:
            raise RuntimeError(
                f"duplicate rule_id {cls.rule_id!r} in default registry — "
                f"both {registry[cls.rule_id].__name__} and {cls.__name__}",
            )
        registry[cls.rule_id] = cls
    return registry


DEFAULT_REGISTRY: RuleRegistry = _build_registry(_RULE_CLASSES)
"""Default mapping of ``rule_id`` to rule class.

Pass to :meth:`~clinical_copilot.discrepancy.engine.DiscrepancyEngine.from_yaml`
unless a caller specifically wants a narrower set.
"""

_PACK_DIR = Path(__file__).resolve().parent

CONSISTENCY_PACK = _PACK_DIR / "consistency.yaml"
DATA_QUALITY_PACK = _PACK_DIR / "data_quality.yaml"
SAFETY_PACK = _PACK_DIR / "safety.yaml"
VALUE_SANITY_PACK = _PACK_DIR / "value_sanity.yaml"

DEFAULT_PACK_PATHS: tuple[Path, ...] = (
    SAFETY_PACK,
    CONSISTENCY_PACK,
    DATA_QUALITY_PACK,
    VALUE_SANITY_PACK,
)
"""Default YAML rule packs.

Safety rides first so its flags appear ahead of consistency and
data-quality in the engine output. Engine ordering today is by
registration order; PR 13d may add explicit severity once eval cases
make the gradient visible.
"""

__all__ = [
    "CONSISTENCY_PACK",
    "DATA_QUALITY_PACK",
    "DEFAULT_PACK_PATHS",
    "DEFAULT_REGISTRY",
    "SAFETY_PACK",
    "VALUE_SANITY_PACK",
    "AllergenMedSafetyConflictRule",
    "LabOutOfRangeRule",
    "MedVsNoteConflictRule",
    "NarrativeOnlyAllergyRule",
    "ResolvedProblemStillActiveRule",
    "StaleChronicLabRule",
]
