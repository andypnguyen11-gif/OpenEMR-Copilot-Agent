"""Rule pack registry — maps YAML rule ``id`` strings to implementing classes.

PR 13b ships only ``med_vs_note_conflict`` to exercise the loader path.
PR 13c adds the rest of the four categories (``data_quality``, ``safety``,
``value_sanity``, plus the remaining ``consistency`` rules) by extending
:data:`DEFAULT_REGISTRY` here and shipping additional YAML packs alongside.
"""

from __future__ import annotations

from pathlib import Path

from clinical_copilot.discrepancy.engine import DiscrepancyRule, RuleRegistry
from clinical_copilot.discrepancy.rules.med_vs_note import MedVsNoteConflictRule

_RULE_CLASSES: tuple[type[DiscrepancyRule], ...] = (MedVsNoteConflictRule,)


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

Pass this to :meth:`~clinical_copilot.discrepancy.engine.DiscrepancyEngine.from_yaml`
unless a caller specifically wants a narrower set of rules.
"""

_PACK_DIR = Path(__file__).resolve().parent

CONSISTENCY_PACK = _PACK_DIR / "consistency.yaml"

DEFAULT_PACK_PATHS: tuple[Path, ...] = (CONSISTENCY_PACK,)
"""Default YAML rule packs shipped with PR 13b.

PR 13c appends ``data_quality.yaml``, ``safety.yaml``, and
``value_sanity.yaml`` so the engine evaluates the full four-category
rule set on every chart.
"""

__all__ = [
    "CONSISTENCY_PACK",
    "DEFAULT_PACK_PATHS",
    "DEFAULT_REGISTRY",
    "MedVsNoteConflictRule",
]
