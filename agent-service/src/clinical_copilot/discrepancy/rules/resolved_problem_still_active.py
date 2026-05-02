"""``resolved_problem_still_active`` — active problem with a recent note
documenting resolution / taper completion.

AUDIT §3.2 cites the case where ``lists`` carries an ``activity=1, no
enddate`` row for hypertension, but a recent note documents BP normalized
off medication and the diagnosis as resolved. The structured row never
got updated and downstream surfaces (problem-list view, chronic-care
flag, USPSTF reminders) treat the patient as still hypertensive.

Heuristic mirrors ``med_vs_note_conflict`` but pivots on
:class:`~clinical_copilot.tools.records.ProblemRecord` instead of
medications, and uses a different keyword set focused on resolution
language.
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


class ResolvedProblemStillActiveRule(DiscrepancyRule):
    """Flags an active problem when a recent note documents resolution."""

    rule_id: ClassVar[str] = "resolved_problem_still_active"
    category: ClassVar[str] = "data_quality"

    DEFAULT_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "resolved",
        "resolution",
        "tapering complete",
        "in remission",
        "remission",
        "no longer active",
    )
    DEFAULT_LOOK_BACK_NOTES: ClassVar[int] = 3

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_keywords = config.params.get("resolution_keywords")
        if raw_keywords is None:
            keywords: tuple[str, ...] = self.DEFAULT_KEYWORDS
        elif isinstance(raw_keywords, list):
            keywords = tuple(str(k).strip().lower() for k in raw_keywords if str(k).strip())
        else:
            raise ValueError(
                f"{self.rule_id}: 'resolution_keywords' must be a list, "
                f"got {type(raw_keywords).__name__}",
            )
        if not keywords:
            raise ValueError(f"{self.rule_id}: 'resolution_keywords' resolved empty")
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

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        if not chart.problems or not chart.notes:
            return []

        recent_notes = sorted(chart.notes, key=lambda n: n.note_date, reverse=True)[
            : self._look_back_notes
        ]
        if not recent_notes:
            return []

        flags: list[FlagRecord] = []
        for problem in chart.problems:
            if problem.status.strip().lower() != "active":
                continue
            token = primary_drug_token(problem.display)
            if not token:
                continue
            for note in recent_notes:
                body_lower = note.body.lower()
                if token not in body_lower:
                    continue
                matched_keyword = self._matched_keyword(body_lower)
                if matched_keyword is None:
                    continue
                ref_ids = [problem.source_id, note.source_id]
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
                            f"Active problem {problem.display!r} but recent "
                            f"note from {note.note_date} mentions "
                            f"{matched_keyword!r}."
                        ),
                        referenced_source_ids=ref_ids,
                    ),
                )
                break
        return flags

    def _matched_keyword(self, body_lower: str) -> str | None:
        for keyword in self._keywords:
            if keyword in body_lower:
                return keyword
        return None

    # Helper kept on the class so tests can introspect look-back if needed.
    def _most_recent(self, notes: Sequence[NoteRecord]) -> Sequence[NoteRecord]:
        return sorted(notes, key=lambda n: n.note_date, reverse=True)[: self._look_back_notes]
