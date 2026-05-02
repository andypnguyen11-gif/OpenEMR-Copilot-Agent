"""``get_problems`` — FHIR ``Condition`` reads.

The tool returns the patient's active and resolved conditions. The
orchestrator anchors any diagnosis claim against one of these records'
``source_id`` so the verification middleware can confirm the cited
record exists.

Projection notes:

* ``onset_date`` flattens the FHIR choice type ``onset[x]``: when
  ``onsetDateTime`` is present it wins; otherwise ``onsetPeriod.start``;
  otherwise ``None``. OpenEMR populates one of the two for ~all
  conditions; the third branch only triggers on imported records that
  predate onset capture.
* ``status`` falls back to the literal string ``"unknown"`` when the
  FHIR resource has no ``clinicalStatus``. The :class:`ProblemRecord`
  schema requires ``status`` because the rules engine (PR 13) keys on
  it for active-vs-resolved logic.
* Conditions whose code has neither a coding nor free text are dropped.
  A record without a code can't be cited meaningfully — surfacing it
  would invite the model to fabricate a display string.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.data.models import Condition
from clinical_copilot.tools.fhir_base import FhirBackedTool, reference_id
from clinical_copilot.tools.records import ProblemRecord

_DEFAULT_STATUS = "unknown"


class GetProblemsFhirTool(FhirBackedTool):
    name: ClassVar[str] = "get_problems"
    description: ClassVar[str] = (
        "Return the patient's problem list (Condition resources). "
        "Use to answer 'what conditions does this patient have?' "
        "and to anchor cited claims about diagnoses."
    )
    required_scope: ClassVar[str] = "system/Condition.read"
    record_kind: ClassVar[str] = "Condition"

    async def _fetch(self, *, patient_id: str) -> Sequence[ProblemRecord]:
        conditions = await self._fhir.search_conditions(patient_id=patient_id)
        records: list[ProblemRecord] = []
        for condition in conditions:
            record = project_condition_to_record(condition)
            if record is not None:
                records.append(record)
        return records


def project_condition_to_record(condition: Condition) -> ProblemRecord | None:
    code = condition.code.primary_code() if condition.code else None
    display = condition.code.preferred_display() if condition.code else None
    if not code or not display:
        return None
    return ProblemRecord(
        source_id=reference_id("Condition", condition.id),
        code=code,
        display=display,
        onset_date=_onset_date(condition),
        status=_status(condition),
    )


def _onset_date(condition: Condition) -> str | None:
    if condition.onset_date_time:
        return condition.onset_date_time
    if condition.onset_period and condition.onset_period.start:
        return condition.onset_period.start
    return None


def _status(condition: Condition) -> str:
    if condition.clinical_status:
        # FHIR puts the status code in coding[].code (e.g. "active",
        # "resolved"); preferred_display walks coding/text and falls
        # through to the code string when there's no display set.
        display = condition.clinical_status.preferred_display()
        if display:
            return display
        primary = condition.clinical_status.primary_code()
        if primary:
            return primary
    return _DEFAULT_STATUS
