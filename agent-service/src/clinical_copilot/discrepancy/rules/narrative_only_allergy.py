"""``narrative_only_allergy`` — allergy mentioned in a note but absent from
:class:`~clinical_copilot.tools.records.AllergyRecord` rows.

AUDIT §3.2 cites the case where an intake form or visit note documents an
allergy (``"reports a sulfa allergy"``) but the structured allergy table
has no matching row, so the safety rule that cross-references active
medications against allergies cannot fire. Surfacing the gap is the
clinician's cue to add the structured row before prescribing.

Heuristic (MVP, AUDIT §3.3 down-scopes regex/NLP):

1. Restrict to the most recent ``look_back_notes`` notes.
2. For each configured ``allergen_keyword`` (``"sulfa"``, ``"penicillin"``,
   ``"peanut"``, ...) check whether the lowercased note body contains the
   keyword *and* a co-occurring marker word (``"allergy"`` or
   ``"allergic"``). Both must appear; ``"sulfa"`` alone is not enough.
3. If it does, check whether ``chart.allergies`` already lists the
   keyword as a substance (case-insensitive substring). If not, emit a
   flag whose ``referenced_source_ids`` cites the offending note only —
   the missing-row absence has no source id to reference.

The rule emits at most one flag per (note, keyword) combination so a
note that mentions two allergens both unlisted produces two flags.
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
from clinical_copilot.tools.records import AllergyRecord, FlagRecord, NoteRecord


class NarrativeOnlyAllergyRule(DiscrepancyRule):
    """Flags an allergy mentioned in a recent note but absent from chart.allergies."""

    rule_id: ClassVar[str] = "narrative_only_allergy"
    category: ClassVar[str] = "consistency"

    DEFAULT_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "sulfa",
        "penicillin",
        "peanut",
        "latex",
        "shellfish",
        "egg",
    )
    DEFAULT_MARKERS: ClassVar[tuple[str, ...]] = ("allergy", "allergic")
    DEFAULT_LOOK_BACK_NOTES: ClassVar[int] = 3

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_keywords = config.params.get("allergen_keywords")
        if raw_keywords is None:
            keywords: tuple[str, ...] = self.DEFAULT_KEYWORDS
        elif isinstance(raw_keywords, list):
            keywords = tuple(str(k).strip().lower() for k in raw_keywords if str(k).strip())
        else:
            raise ValueError(
                f"{self.rule_id}: 'allergen_keywords' must be a list, "
                f"got {type(raw_keywords).__name__}",
            )
        if not keywords:
            raise ValueError(f"{self.rule_id}: 'allergen_keywords' resolved empty")
        self._keywords = keywords

        raw_markers = config.params.get("marker_words", self.DEFAULT_MARKERS)
        if not isinstance(raw_markers, list | tuple):
            raise ValueError(
                f"{self.rule_id}: 'marker_words' must be a list, got {type(raw_markers).__name__}",
            )
        markers = tuple(str(m).strip().lower() for m in raw_markers if str(m).strip())
        if not markers:
            raise ValueError(f"{self.rule_id}: 'marker_words' resolved empty")
        self._markers = markers

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

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        if not chart.notes:
            return []

        recent_notes = self._most_recent(chart.notes)
        if not recent_notes:
            return []

        flags: list[FlagRecord] = []
        for note in recent_notes:
            body_lower = note.body.lower()
            if not any(marker in body_lower for marker in self._markers):
                continue
            for keyword in self._keywords:
                if keyword not in body_lower:
                    continue
                if self._already_listed(keyword, chart.allergies):
                    continue
                ref_ids = [note.source_id]
                flags.append(
                    FlagRecord(
                        source_id=flag_source_id(
                            rule_id=self.rule_id,
                            patient_id=chart.patient_id,
                            referenced_source_ids=[*ref_ids, keyword],
                        ),
                        rule_id=self.rule_id,
                        category=self.category,
                        rationale=(
                            f"Note from {note.note_date} mentions a {keyword!r} "
                            f"allergy but no allergy row for {keyword!r} exists "
                            f"in the chart."
                        ),
                        referenced_source_ids=ref_ids,
                    ),
                )
        return flags

    def _most_recent(self, notes: Sequence[NoteRecord]) -> Sequence[NoteRecord]:
        ordered = sorted(notes, key=lambda n: n.note_date, reverse=True)
        return ordered[: self._look_back_notes]

    @staticmethod
    def _already_listed(keyword: str, allergies: Sequence[AllergyRecord]) -> bool:
        keyword_lower = keyword.lower()
        return any(keyword_lower in entry.substance.lower() for entry in allergies)
