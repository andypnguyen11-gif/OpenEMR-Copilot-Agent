"""``allergen_med_safety_conflict`` — active medication that an allergy
on the chart should preclude.

AUDIT §3.2 cites the Penicillin-allergy + active-Amoxicillin case as a
patient-safety wipeout: cross-reactivity between the listed allergen and
a structurally related drug is the kind of conflict the chart must
surface, and a clinician glancing at the medication list alone might
miss it. The rule is the most consequential one in the engine — its
flag should always show ahead of the data-quality and consistency rules
when it fires (engine ordering today is by registry position; PR 13d
may add an explicit severity).

Match logic:

1. Build a normalized lookup over ``chart.allergies`` keyed by primary
   drug token (``"penicillin"``, ``"sulfa"``, ...).
2. For each ``"active"`` medication, take its primary token. Look it up
   in the cross-reactivity table to expand the implicated allergen
   classes (``"amoxicillin"`` → expands to allergen class ``"penicillin"``).
3. If any expanded class is in the allergy lookup, emit a flag whose
   ``referenced_source_ids`` cite both the allergy and the medication.

The cross-reactivity table is config (YAML), not code — adding a new
allergen class is a YAML edit. Default tables ship with the most common
beta-lactam and sulfa overlaps; extend in the YAML pack as eval cases
expose gaps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from clinical_copilot.discrepancy.engine import (
    DiscrepancyRule,
    PatientChart,
    RuleConfig,
    flag_source_id,
)
from clinical_copilot.discrepancy.normalize import primary_drug_token
from clinical_copilot.tools.records import FlagRecord, MedicationRecord


class AllergenMedSafetyConflictRule(DiscrepancyRule):
    """Flags an active medication that overlaps a charted allergy."""

    rule_id: ClassVar[str] = "allergen_med_safety_conflict"
    category: ClassVar[str] = "safety"

    # ``med_token`` -> ``allergen_class`` so a med name resolves directly
    # to the allergen we should look up in the patient's allergy list.
    # Exact-match keys; extend the YAML config to broaden coverage.
    DEFAULT_MED_TO_ALLERGEN_CLASS: ClassVar[Mapping[str, str]] = {
        "amoxicillin": "penicillin",
        "ampicillin": "penicillin",
        "augmentin": "penicillin",
        "dicloxacillin": "penicillin",
        "penicillin": "penicillin",
        "bactrim": "sulfa",
        "sulfamethoxazole": "sulfa",
        "trimethoprim": "sulfa",
        "cephalexin": "cephalosporin",
        "keflex": "cephalosporin",
    }

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_table = config.params.get("med_to_allergen_class")
        if raw_table is None:
            table: Mapping[str, str] = self.DEFAULT_MED_TO_ALLERGEN_CLASS
        elif isinstance(raw_table, dict):
            table = {
                str(k).strip().lower(): str(v).strip().lower()
                for k, v in raw_table.items()
                if str(k).strip() and str(v).strip()
            }
        else:
            raise ValueError(
                f"{self.rule_id}: 'med_to_allergen_class' must be a mapping, "
                f"got {type(raw_table).__name__}",
            )
        if not table:
            raise ValueError(f"{self.rule_id}: 'med_to_allergen_class' resolved empty")
        self._med_to_allergen = dict(table)

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        if not chart.allergies or not chart.medications:
            return []

        # Map the patient's allergens to their source records so we can
        # both confirm a substance is listed and cite the right id.
        allergen_lookup: dict[str, str] = {}
        for allergy in chart.allergies:
            token = primary_drug_token(allergy.substance)
            if not token:
                continue
            # First-write-wins so a duplicate allergy entry doesn't change
            # which source_id we cite (deterministic across runs).
            allergen_lookup.setdefault(token, allergy.source_id)

        flags: list[FlagRecord] = []
        for med in chart.medications:
            if med.status.strip().lower() != "active":
                continue
            med_token = primary_drug_token(med.name)
            if not med_token:
                continue

            # Direct match — patient is allergic to a med they're taking
            # under its own name.
            if med_token in allergen_lookup:
                flags.append(
                    self._make_flag(
                        chart_patient_id=chart.patient_id,
                        allergy_source_id=allergen_lookup[med_token],
                        med=med,
                        allergen_class=med_token,
                    ),
                )
                continue

            # Cross-reactivity match — med token resolves to an allergen
            # class the patient is listed as allergic to.
            allergen_class = self._med_to_allergen.get(med_token)
            if allergen_class is None:
                continue
            allergy_source_id = allergen_lookup.get(allergen_class)
            if allergy_source_id is None:
                continue

            flags.append(
                self._make_flag(
                    chart_patient_id=chart.patient_id,
                    allergy_source_id=allergy_source_id,
                    med=med,
                    allergen_class=allergen_class,
                ),
            )
        return flags

    def _make_flag(
        self,
        *,
        chart_patient_id: str,
        allergy_source_id: str,
        med: MedicationRecord,
        allergen_class: str,
    ) -> FlagRecord:
        med_source_id = med.source_id
        med_name = med.name
        ref_ids = [allergy_source_id, med_source_id]
        return FlagRecord(
            source_id=flag_source_id(
                rule_id=self.rule_id,
                patient_id=chart_patient_id,
                referenced_source_ids=ref_ids,
            ),
            rule_id=self.rule_id,
            category=self.category,
            rationale=(
                f"Active medication {med_name!r} conflicts with charted {allergen_class!r} allergy."
            ),
            referenced_source_ids=ref_ids,
        )
