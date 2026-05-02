"""``lab_out_of_plausible_range`` — value-sanity guard for lab results.

The seeded scenarios from PR 13a include a stale-but-otherwise-plausible
HbA1c of 7.8 (above the 4.0-5.6 reference range, but not clinically
implausible). The MVP value-sanity rule deliberately fires only on the
``"panic"`` / ``"critical"`` / ``"extreme"`` abnormality severities —
the codes a lab uses for results that are out-of-range *and* outside any
reasonable physiological window — so the seeded fixture does not trip
this rule. PR 13c uses it primarily to round out the four-category rule
set; PR 13d extends it once eval cases identify the value bands worth
catching.
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
from clinical_copilot.tools.records import FlagRecord


class LabOutOfRangeRule(DiscrepancyRule):
    """Flags labs whose abnormal severity matches a configured panic level."""

    rule_id: ClassVar[str] = "lab_out_of_plausible_range"
    category: ClassVar[str] = "value_sanity"

    DEFAULT_SEVERITIES: ClassVar[tuple[str, ...]] = ("panic", "critical", "extreme")

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)

        raw_severities = config.params.get("abnormal_severities")
        if raw_severities is None:
            severities: tuple[str, ...] = self.DEFAULT_SEVERITIES
        elif isinstance(raw_severities, list):
            severities = tuple(str(s).strip().lower() for s in raw_severities if str(s).strip())
        else:
            raise ValueError(
                f"{self.rule_id}: 'abnormal_severities' must be a list, "
                f"got {type(raw_severities).__name__}",
            )
        if not severities:
            raise ValueError(f"{self.rule_id}: 'abnormal_severities' resolved empty")
        self._severities = severities

    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        flags: list[FlagRecord] = []
        for lab in chart.labs:
            severity = (lab.abnormal if hasattr(lab, "abnormal") else "").strip().lower()
            # ``LabRecord`` does not currently carry an ``abnormal`` field;
            # the seeded SQL fixture does, so downstream tools that
            # populate the field can extend the record without code edits
            # here. For now, gracefully no-op when the attribute is absent.
            if not severity or severity not in self._severities:
                continue
            ref_ids = [lab.source_id]
            flags.append(
                FlagRecord(
                    source_id=flag_source_id(
                        rule_id=self.rule_id,
                        patient_id=chart.patient_id,
                        referenced_source_ids=ref_ids,
                    ),
                    rule_id=self.rule_id,
                    category=self.category,
                    rationale=(f"Lab {lab.display!r} flagged {severity!r} on {lab.observed_on}."),
                    referenced_source_ids=ref_ids,
                ),
            )
        return flags
