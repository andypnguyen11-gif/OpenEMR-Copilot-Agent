"""Human-reviewed label schema for the Stage 4A extraction eval.

Each case in ``cases.jsonl`` references a label JSON under
``evals/extraction/labels/<bucket>/<case_id>.json``. The label expresses
*what the case asserts*, decoupled from any rubric implementation
detail. The runner walks the label structure to evaluate each rubric.

The schema favours **required-field** labels (specific field paths to
check) over full-snapshot labels. Snapshots are brittle against the
extractor's per-field abstention behaviour and break every time the
schema gains an optional field.

Required structure (top-level):

* ``metadata`` — provenance. ``review_status`` MUST be
  ``"human_reviewed"`` for the case to be admissible. ``reviewed_by``
  and ``reviewed_at`` are recorded for audit but not required to be
  non-empty in unit tests.
* ``required_fields`` — list of dotted field paths and their expected
  values. Strings compare case-insensitively after a normalize step.
  Numbers compare with absolute tolerance (default 0.01, override
  per-row via ``"tolerance"``).
* ``must_abstain`` — paths whose ``ExtractedField.abstain_reason`` MUST
  match the listed :class:`RuntimeAbstainReason`. The path here points
  at the field (not at ``.abstain_reason``); the rubric looks one level
  deeper.
* ``required_citations`` — list of dotted paths whose
  ``ExtractedField.citation`` must validate (non-degenerate bbox,
  confidence in [0,1], non-empty raw_text, page >= 1).
* ``expected_retrieval`` — for retrieval-bucket cases. Asserts gold
  ``source_doc_id`` (or ``chunk_id``) appears in top-k. ``k`` defaults
  to 5.
* ``safe_refusal`` — for refusal/missing-data buckets. Asserts the
  expected abstention reason and a list of forbidden fact patterns
  that must not appear anywhere in the synthesized output.

Every section is optional except ``metadata``. A valid label may carry
just ``required_fields`` (most extraction cases), just
``expected_retrieval`` (retrieval cases), or just ``safe_refusal``
(safe-refusal cases).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clinical_copilot.schemas.abstain import RuntimeAbstainReason


class LabelError(ValueError):
    """Raised when a label JSON fails schema validation."""


@dataclass(frozen=True, slots=True)
class RequiredField:
    """One ``required_fields`` row."""

    path: str
    expected: object
    tolerance: float = 0.01


@dataclass(frozen=True, slots=True)
class RequiredListMin:
    """One ``required_list_min`` row.

    Asserts that the list at ``path`` has at least ``min_count`` entries.
    Used by intake-form cases that need to express row-count thresholds
    on ``current_medications`` / ``reported_allergies`` /
    ``active_problems`` / ``family_history``.
    """

    path: str
    min_count: int


@dataclass(frozen=True, slots=True)
class MustAbstain:
    """One ``must_abstain`` row."""

    path: str
    reason: RuntimeAbstainReason


@dataclass(frozen=True, slots=True)
class RequiredCitation:
    """One ``required_citations`` row.

    ``path`` resolves to the *field* whose ``.citation`` must validate
    (e.g. ``observations[0].value``); the rubric looks at
    ``<path>.citation``.
    """

    path: str


@dataclass(frozen=True, slots=True)
class ExpectedRetrieval:
    """``expected_retrieval`` block for retrieval cases."""

    expected_source_doc_ids: tuple[str, ...]
    expected_chunk_ids: tuple[str, ...] = ()
    k: int = 5


@dataclass(frozen=True, slots=True)
class SafeRefusal:
    """``safe_refusal`` block for refusal / missing-data cases."""

    expected_reason: RuntimeAbstainReason | None
    forbidden_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelMetadata:
    """``metadata`` block. ``review_status`` enforced upstream."""

    review_status: str
    reviewed_by: str = ""
    reviewed_at: str = ""
    source_notes: str = ""


@dataclass(frozen=True, slots=True)
class Label:
    metadata: LabelMetadata
    required_fields: tuple[RequiredField, ...] = ()
    required_field_present: tuple[str, ...] = ()
    """Field paths that must resolve to a non-null value. Used when the
    exact value isn't asserted but presence is required (e.g.
    ``chief_complaint.value`` for an intake form whose free-text content
    varies but must always be extracted)."""

    required_list_min: tuple[RequiredListMin, ...] = ()
    must_abstain: tuple[MustAbstain, ...] = ()
    required_citations: tuple[RequiredCitation, ...] = ()
    expected_retrieval: ExpectedRetrieval | None = None
    safe_refusal: SafeRefusal | None = None
    forbidden_patterns_global: tuple[str, ...] = field(default_factory=tuple)


REVIEW_STATUS_REVIEWED = "human_reviewed"


def load_label(path: Path) -> Label:
    """Read a label JSON, validate, and return :class:`Label`.

    ``review_status`` MUST equal ``"human_reviewed"`` — unreviewed
    labels fail the gate before any rubric runs. The runner converts
    this error into a non-zero exit so an accidentally-committed AI
    draft cannot pass CI.
    """

    raw = json.loads(path.read_text())

    md_raw = raw.get("metadata") or {}
    md = LabelMetadata(
        review_status=str(md_raw.get("review_status") or ""),
        reviewed_by=str(md_raw.get("reviewed_by") or ""),
        reviewed_at=str(md_raw.get("reviewed_at") or ""),
        source_notes=str(md_raw.get("source_notes") or ""),
    )
    if md.review_status != REVIEW_STATUS_REVIEWED:
        raise LabelError(
            f"label {path} not human-reviewed "
            f"(metadata.review_status={md.review_status!r}); refusing to admit"
        )

    return Label(
        metadata=md,
        required_fields=_parse_required_fields(raw.get("required_fields") or []),
        required_field_present=tuple(str(x) for x in (raw.get("required_field_present") or [])),
        required_list_min=_parse_required_list_min(raw.get("required_list_min") or []),
        must_abstain=_parse_must_abstain(raw.get("must_abstain") or []),
        required_citations=_parse_required_citations(raw.get("required_citations") or []),
        expected_retrieval=_parse_expected_retrieval(raw.get("expected_retrieval")),
        safe_refusal=_parse_safe_refusal(raw.get("safe_refusal")),
    )


def _parse_required_list_min(rows: list[Any]) -> tuple[RequiredListMin, ...]:
    out: list[RequiredListMin] = []
    for row in rows:
        if not isinstance(row, dict) or "path" not in row or "min_count" not in row:
            raise LabelError(f"required_list_min row malformed: {row!r}")
        out.append(RequiredListMin(path=str(row["path"]), min_count=int(row["min_count"])))
    return tuple(out)


def _parse_required_fields(rows: list[Any]) -> tuple[RequiredField, ...]:
    out: list[RequiredField] = []
    for row in rows:
        if not isinstance(row, dict) or "path" not in row:
            raise LabelError(f"required_fields row missing 'path': {row!r}")
        out.append(
            RequiredField(
                path=str(row["path"]),
                expected=row.get("expected"),
                tolerance=float(row.get("tolerance", 0.01)),
            )
        )
    return tuple(out)


def _parse_must_abstain(rows: list[Any]) -> tuple[MustAbstain, ...]:
    out: list[MustAbstain] = []
    for row in rows:
        if not isinstance(row, dict) or "path" not in row or "reason" not in row:
            raise LabelError(f"must_abstain row malformed: {row!r}")
        try:
            reason = RuntimeAbstainReason(row["reason"])
        except ValueError as exc:
            raise LabelError(f"must_abstain reason invalid: {row!r}") from exc
        out.append(MustAbstain(path=str(row["path"]), reason=reason))
    return tuple(out)


def _parse_required_citations(rows: list[Any]) -> tuple[RequiredCitation, ...]:
    out: list[RequiredCitation] = []
    for row in rows:
        if not isinstance(row, dict) or "path" not in row:
            raise LabelError(f"required_citations row missing 'path': {row!r}")
        out.append(RequiredCitation(path=str(row["path"])))
    return tuple(out)


def _parse_expected_retrieval(raw: object) -> ExpectedRetrieval | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LabelError(f"expected_retrieval not a dict: {raw!r}")
    return ExpectedRetrieval(
        expected_source_doc_ids=tuple(str(x) for x in raw.get("expected_source_doc_ids") or []),
        expected_chunk_ids=tuple(str(x) for x in raw.get("expected_chunk_ids") or []),
        k=int(raw.get("k", 5)),
    )


def _parse_safe_refusal(raw: object) -> SafeRefusal | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LabelError(f"safe_refusal not a dict: {raw!r}")
    expected_reason_raw = raw.get("expected_reason")
    expected_reason: RuntimeAbstainReason | None = None
    if expected_reason_raw is not None:
        try:
            expected_reason = RuntimeAbstainReason(expected_reason_raw)
        except ValueError as exc:
            raise LabelError(
                f"safe_refusal.expected_reason invalid: {expected_reason_raw!r}"
            ) from exc
    return SafeRefusal(
        expected_reason=expected_reason,
        forbidden_patterns=tuple(str(x) for x in raw.get("forbidden_patterns") or []),
    )
