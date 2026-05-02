"""``med_vs_note_conflict`` — active medication contradicted by a recent note.

Detection heuristic (per AUDIT §3.3 — keyword-on-most-recent-notes only;
no NLP / regex over the full note corpus for MVP):

1. Restrict candidate notes to the ``look_back_notes`` most recent entries
   ordered by ``note_date`` descending.
2. For each :class:`MedicationRecord` whose ``status`` is ``"active"``, take
   its primary drug token (the leading generic stem, dose-stripped — see
   :func:`~clinical_copilot.discrepancy.normalize.primary_drug_token`).
3. If that token appears anywhere in a candidate note's lowercased body
   *and* one of the configured conflict keywords (``"discontinued"``,
   ``"stopped"``, ...) also appears in the same body, emit a flag whose
   ``referenced_source_ids`` cite both the medication and the note.

The keyword-anywhere-in-body check is deliberately coarse for MVP: a more
precise proximity check (keyword within N chars of the drug token) lands
later if the false-positive rate becomes a problem in eval. The rule
emits at most one flag per medication; the verification middleware does
not benefit from duplicates.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.discrepancy.engine import (
    DiscrepancyRule,
    PatientChart,
    RuleConfig,
    flag_source_id,
)
from clinical_copilot.discrepancy.normalize import primary_drug_token
from clinical_copilot.tools.records import FlagRecord, NoteRecord


class MedVsNoteConflictRule(DiscrepancyRule):
    """Flags an active medication when a recent note documents stopping it."""

    rule_id: ClassVar[str] = "med_vs_note_conflict"
    category: ClassVar[str] = "consistency"

    DEFAULT_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "discontinued",
        "stopped",
        "held",
        "tapered",
        "taper",
        "ceased",
    )
    DEFAULT_LOOK_BACK_NOTES: ClassVar[int] = 3

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_keywords = config.params.get("conflict_keywords")
        if raw_keywords is None:
            keywords: tuple[str, ...] = self.DEFAULT_KEYWORDS
        elif isinstance(raw_keywords, list):
            keywords = tuple(str(k).strip().lower() for k in raw_keywords if str(k).strip())
        else:
            raise ValueError(
                f"{self.rule_id}: 'conflict_keywords' must be a list, "
                f"got {type(raw_keywords).__name__}",
            )
        if not keywords:
            raise ValueError(f"{self.rule_id}: 'conflict_keywords' resolved empty")
        self._keywords = keywords

        raw_look_back = config.params.get("look_back_notes", self.DEFAULT_LOOK_BACK_NOTES)
        if (
            not isinstance(raw_look_back, int)
            or isinstance(raw_look_back, bool)
            or raw_look_back < 1
        ):
            raise ValueError(
                f"{self.rule_id}: 'look_back_notes' must be a positive int, got {raw_look_back!r}",
            )
        self._look_back_notes = raw_look_back

    @property
    def keywords(self) -> tuple[str, ...]:
        return self._keywords

    @property
    def look_back_notes(self) -> int:
        return self._look_back_notes

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        if not chart.medications or not chart.notes:
            return []

        recent_notes = self._most_recent_notes(chart.notes)
        if not recent_notes:
            return []

        flags: list[FlagRecord] = []
        for med in chart.medications:
            if med.status.strip().lower() != "active":
                continue
            token = primary_drug_token(med.name)
            if not token:
                continue
            for note in recent_notes:
                body_lower = note.body.lower()
                if token not in body_lower:
                    continue
                matched_keyword = next(
                    (kw for kw in self._keywords if kw in body_lower),
                    None,
                )
                if matched_keyword is None:
                    continue
                ref_ids = [med.source_id, note.source_id]
                flags.append(
                    FlagRecord(
                        source_id=flag_source_id(
                            rule_id=self.rule_id,
                            patient_id=chart.patient_id,
                            referenced_source_ids=ref_ids,
                        ),
                        rule_id=self.rule_id,
                        category=self.category,
                        rationale=(
                            f"Active medication {med.name!r} but recent "
                            f"note from {note.note_date} mentions "
                            f"{matched_keyword!r}."
                        ),
                        referenced_source_ids=ref_ids,
                    ),
                )
                break  # one flag per medication, even if multiple notes match
        return flags

    def _most_recent_notes(
        self,
        notes: Sequence[NoteRecord],
    ) -> Sequence[NoteRecord]:
        # Stable sort by note_date desc; ties preserve input order so the
        # output stays deterministic across runs (tests pin this).
        ordered = sorted(notes, key=lambda n: n.note_date, reverse=True)
        return ordered[: self._look_back_notes]
