"""``stale_chronic_lab`` — chronic problem with no recent monitoring lab.

AUDIT §3.2 cites the Type 2 Diabetes case where the problem list shows
the diagnosis but the most recent HbA1c is over 12 months old. The
clinical reminder ("get an A1c") is the chart's responsibility to surface
even when the patient is otherwise asymptomatic, and missing it is a
data-quality failure that downstream reminders rely on the engine to
catch.

Each chronic-problem-to-lab mapping in the YAML config carries:

* ``problem_keywords`` — substrings (case-insensitive) matched against
  ``ProblemRecord.display`` and ``ProblemRecord.code`` so the rule fires
  on either the human label (``"Type 2 Diabetes Mellitus"``) or the ICD
  code (``"E11.9"``).
* ``expected_lab_codes`` — LOINC codes the rule expects to see at least
  one recent result for. The rule looks at ``LabRecord.code`` *and*
  ``LabRecord.display`` so a free-text label like ``"Hemoglobin A1c"``
  matches alongside the LOINC ``"4548-4"``.
* ``max_age_months`` — labs older than this (relative to ``as_of``)
  count as stale.

The reference date (``as_of``) is configurable so tests stay
deterministic and reproducible without coupling to ``datetime.today``.
Production wiring may either pin a per-request value (tied to the
visit / brief generation timestamp) or leave it unset to default to
today.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, ClassVar

from clinical_copilot.discrepancy.engine import (
    DiscrepancyRule,
    PatientChart,
    RuleConfig,
    flag_source_id,
)
from clinical_copilot.tools.records import FlagRecord, LabRecord, ProblemRecord


class _Expectation:
    """Frozen-after-construction wrapper for one chronic-problem mapping."""

    __slots__ = ("expected_lab_codes", "max_age_months", "problem_keywords")

    def __init__(
        self,
        *,
        problem_keywords: tuple[str, ...],
        expected_lab_codes: tuple[str, ...],
        max_age_months: int,
    ) -> None:
        self.problem_keywords = problem_keywords
        self.expected_lab_codes = expected_lab_codes
        self.max_age_months = max_age_months


class StaleChronicLabRule(DiscrepancyRule):
    """Flags a chronic problem when the expected monitoring lab is stale."""

    rule_id: ClassVar[str] = "stale_chronic_lab"
    category: ClassVar[str] = "data_quality"

    DEFAULT_EXPECTATIONS: ClassVar[tuple[Mapping[str, list[str] | int], ...]] = (
        {
            "problem_keywords": [
                "type 2 diabetes",
                "type ii diabetes",
                "diabetes mellitus",
                "t2dm",
                "e11",
            ],
            "expected_lab_codes": ["4548-4", "hemoglobin a1c", "hba1c"],
            "max_age_months": 12,
        },
    )

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_expectations = config.params.get("expectations", self.DEFAULT_EXPECTATIONS)
        if not isinstance(raw_expectations, list | tuple) or not raw_expectations:
            raise ValueError(
                f"{self.rule_id}: 'expectations' must be a non-empty list",
            )
        self._expectations = tuple(self._parse_expectation(e) for e in raw_expectations)

        raw_as_of = config.params.get("as_of")
        if raw_as_of is None:
            self._as_of: date | None = None
        elif isinstance(raw_as_of, str):
            self._as_of = date.fromisoformat(raw_as_of)
        elif isinstance(raw_as_of, date):
            self._as_of = raw_as_of
        else:
            raise ValueError(
                f"{self.rule_id}: 'as_of' must be a date or ISO-8601 string, "
                f"got {type(raw_as_of).__name__}",
            )

    @staticmethod
    def _parse_expectation(raw: Any) -> _Expectation:
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"stale_chronic_lab: each expectation must be a mapping, got {type(raw).__name__}",
            )
        problem_keywords_raw = raw.get("problem_keywords")
        expected_codes_raw = raw.get("expected_lab_codes")
        max_age_raw = raw.get("max_age_months")
        if not isinstance(problem_keywords_raw, list) or not problem_keywords_raw:
            raise ValueError(
                "stale_chronic_lab: 'problem_keywords' must be a non-empty list",
            )
        if not isinstance(expected_codes_raw, list) or not expected_codes_raw:
            raise ValueError(
                "stale_chronic_lab: 'expected_lab_codes' must be a non-empty list",
            )
        if not isinstance(max_age_raw, int) or isinstance(max_age_raw, bool) or max_age_raw < 1:
            raise ValueError(
                "stale_chronic_lab: 'max_age_months' must be a positive int",
            )
        return _Expectation(
            problem_keywords=tuple(
                str(k).strip().lower() for k in problem_keywords_raw if str(k).strip()
            ),
            expected_lab_codes=tuple(
                str(c).strip().lower() for c in expected_codes_raw if str(c).strip()
            ),
            max_age_months=max_age_raw,
        )

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        if not chart.problems:
            return []
        as_of = self._as_of if self._as_of is not None else date.today()

        flags: list[FlagRecord] = []
        for problem in chart.problems:
            if problem.status.strip().lower() != "active":
                continue
            for expectation in self._expectations:
                if not _problem_matches(problem, expectation.problem_keywords):
                    continue
                most_recent = _most_recent_matching_lab(
                    chart.labs,
                    expectation.expected_lab_codes,
                )
                if most_recent is None:
                    flags.append(
                        self._build_flag(
                            chart=chart,
                            problem=problem,
                            lab=None,
                            reason="no matching lab on chart",
                        ),
                    )
                    break
                lab_date = _coerce_iso_date(most_recent.observed_on)
                if lab_date is None:
                    # Skip labs whose observed_on is unparsable rather
                    # than emit a false stale flag.
                    continue
                age_months = _months_between(lab_date, as_of)
                if age_months > expectation.max_age_months:
                    flags.append(
                        self._build_flag(
                            chart=chart,
                            problem=problem,
                            lab=most_recent,
                            reason=(
                                f"last lab {age_months} months old "
                                f"(threshold {expectation.max_age_months})"
                            ),
                        ),
                    )
                break
        return flags

    def _build_flag(
        self,
        *,
        chart: PatientChart,
        problem: ProblemRecord,
        lab: LabRecord | None,
        reason: str,
    ) -> FlagRecord:
        ref_ids: list[str] = [problem.source_id]
        if lab is not None:
            ref_ids.append(lab.source_id)
        return FlagRecord(
            source_id=flag_source_id(
                rule_id=self.rule_id,
                patient_id=chart.patient_id,
                referenced_source_ids=ref_ids,
            ),
            rule_id=self.rule_id,
            category=self.category,
            rationale=(f"Chronic problem {problem.display!r}: {reason}."),
            referenced_source_ids=ref_ids,
        )


def _problem_matches(problem: ProblemRecord, keywords: Sequence[str]) -> bool:
    haystacks: tuple[str, ...] = (problem.display.lower(),)
    if problem.code is not None:
        haystacks = (*haystacks, problem.code.lower())
    for keyword in keywords:
        for haystack in haystacks:
            if keyword in haystack:
                return True
    return False


def _most_recent_matching_lab(
    labs: Sequence[LabRecord],
    expected_codes: Sequence[str],
) -> LabRecord | None:
    matches = [lab for lab in labs if _lab_matches(lab, expected_codes)]
    if not matches:
        return None
    # Sort by observed_on descending; ties preserve input order via
    # stable sort so the citation stays deterministic across runs.
    matches.sort(key=lambda lab: lab.observed_on, reverse=True)
    return matches[0]


def _lab_matches(lab: LabRecord, expected_codes: Sequence[str]) -> bool:
    haystacks = (lab.code.lower(), lab.display.lower())
    for expected in expected_codes:
        for haystack in haystacks:
            if expected in haystack:
                return True
    return False


def _coerce_iso_date(value: str) -> date | None:
    """Parse an ISO 8601 date or datetime prefix into a ``date``."""

    if not value:
        return None
    head = value.split("T", 1)[0].split(" ", 1)[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def _months_between(earlier: date, later: date) -> int:
    """Approximate calendar-month delta between two dates.

    Rounds toward zero and ignores day-of-month — the rule only cares
    about coarse "older than N months" thresholds, so the trade-off is
    safe.
    """

    if later < earlier:
        return 0
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)
