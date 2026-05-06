"""Unit smoke for the Stage 4A eval harness.

Covers the load-time invariants (exact-50, duplicate detection, missing
artifact detection, unreviewed-label rejection) and one passing-rubric
fixture per category. The full per-rubric coverage is the integration
smoke (``test_eval_gate.py``) and the live ``make eval-extraction-gate``
run; this module is the fast-feedback unit tier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinical_copilot.evals.extraction.cases import (
    Bucket,
    CaseManifestError,
    DocumentType,
    RubricCategory,
    load_cases,
)
from clinical_copilot.evals.extraction.labels import (
    Label,
    LabelError,
    LabelMetadata,
    RequiredField,
    load_label,
)
from clinical_copilot.evals.extraction.rubrics import (
    EvalOutput,
    RubricOutcome,
    run_rubrics,
)

# --------------------------------------------------------------- helpers


def _write_label(path: Path, *, reviewed: bool, **body: object) -> None:
    payload: dict[str, object] = {
        "metadata": {
            "review_status": "human_reviewed" if reviewed else "draft",
            "reviewed_by": "tester",
            "reviewed_at": "2026-05-06",
        },
        **body,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# --------------------------------------------------------------- loader


def test_load_cases_rejects_partial_count(tmp_path: Path) -> None:
    label = tmp_path / "labels" / "extraction" / "x.json"
    _write_label(label, reviewed=True)
    manifest = tmp_path / "cases.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "case_id": "extraction/x",
                "bucket": "extraction",
                "label_path": "labels/extraction/x.json",
                "rubric_categories": ["schema_valid"],
                "description": "single case",
            }
        ],
    )

    with pytest.raises(CaseManifestError, match="exactly 50"):
        load_cases(manifest)


def test_load_cases_allow_partial_relaxes_count(tmp_path: Path) -> None:
    label = tmp_path / "labels" / "extraction" / "x.json"
    _write_label(label, reviewed=True)
    manifest = tmp_path / "cases.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "case_id": "extraction/x",
                "bucket": "extraction",
                "label_path": "labels/extraction/x.json",
                "rubric_categories": ["schema_valid"],
                "description": "single case",
            }
        ],
    )
    cases = load_cases(manifest, allow_partial=True)
    assert len(cases) == 1
    assert cases[0].case_id == "extraction/x"


def test_load_cases_rejects_duplicate_case_id(tmp_path: Path) -> None:
    label = tmp_path / "labels" / "extraction" / "x.json"
    _write_label(label, reviewed=True)
    manifest = tmp_path / "cases.jsonl"
    row = {
        "case_id": "extraction/x",
        "bucket": "extraction",
        "label_path": "labels/extraction/x.json",
        "rubric_categories": ["schema_valid"],
        "description": "dup",
    }
    _write_manifest(manifest, [row, row])

    with pytest.raises(CaseManifestError, match="duplicate case_id"):
        load_cases(manifest, allow_partial=True)


def test_load_cases_rejects_missing_label_path(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "case_id": "extraction/x",
                "bucket": "extraction",
                "label_path": "labels/extraction/missing.json",
                "rubric_categories": ["schema_valid"],
                "description": "missing label",
            }
        ],
    )
    with pytest.raises(CaseManifestError, match="label_path missing"):
        load_cases(manifest, allow_partial=True)


# --------------------------------------------------------------- labels


def test_load_label_rejects_unreviewed(tmp_path: Path) -> None:
    label_path = tmp_path / "label.json"
    _write_label(label_path, reviewed=False)
    with pytest.raises(LabelError, match="not human-reviewed"):
        load_label(label_path)


def test_load_label_parses_required_fields(tmp_path: Path) -> None:
    label_path = tmp_path / "label.json"
    _write_label(
        label_path,
        reviewed=True,
        required_fields=[
            {"path": "observations[0].value.value", "expected": 110.0, "tolerance": 0.5}
        ],
    )
    label = load_label(label_path)
    assert label.required_fields == (
        RequiredField(path="observations[0].value.value", expected=110.0, tolerance=0.5),
    )


# --------------------------------------------------------------- rubrics


def _stub_case(rubric: RubricCategory, bucket: Bucket = Bucket.EXTRACTION):
    from clinical_copilot.evals.extraction.cases import Case

    return Case(
        case_id="t/x",
        bucket=bucket,
        document_type=DocumentType.LAB_PDF if bucket is Bucket.EXTRACTION else None,
        document_path=None,
        query=None,
        label_path=Path("/dev/null"),
        prediction_path=None,
        rubric_categories=(rubric,),
        live_smoke=False,
        description="",
    )


def _empty_label() -> Label:
    return Label(metadata=LabelMetadata(review_status="human_reviewed"))


def test_rubric_no_phi_in_logs_passes_clean_output() -> None:
    case = _stub_case(RubricCategory.NO_PHI_IN_LOGS)
    output = EvalOutput(synthesized_text="patient has hypertension")
    outcomes = run_rubrics(case, _empty_label(), output)
    assert outcomes == [RubricOutcome(rubric=RubricCategory.NO_PHI_IN_LOGS, passed=True, reason="")]


def test_rubric_no_phi_in_logs_catches_ssn() -> None:
    case = _stub_case(RubricCategory.NO_PHI_IN_LOGS)
    output = EvalOutput(synthesized_text="patient SSN 123-45-6789 on file")
    [outcome] = run_rubrics(case, _empty_label(), output)
    assert outcome.passed is False
    assert "SSN" in outcome.reason


def test_rubric_no_phi_in_logs_catches_forbidden_token() -> None:
    case = _stub_case(RubricCategory.NO_PHI_IN_LOGS)
    output = EvalOutput(
        synthesized_text="hello Dr. Smith",
        forbidden_phi=("Dr. Smith",),
    )
    [outcome] = run_rubrics(case, _empty_label(), output)
    assert outcome.passed is False
    assert "Dr. Smith" in outcome.reason
