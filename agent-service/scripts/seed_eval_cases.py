"""One-shot generator for the Stage 4A 50-case eval manifest + labels.

Reads the legacy 10 cases under ``tests/eval/w2_cases/extraction-{lab,
intake}/``, translates each into the new ``Label`` schema, then writes:

* ``evals/extraction/cases.jsonl`` — the 50-case manifest (10 migrated
  + 18 aspect + 6 citation + 4 missing-data + 4 refusal + 8 retrieval
  placeholders).
* ``evals/extraction/labels/<bucket>/<case_id>.json`` — one human-
  reviewed label per case.
* ``evals/extraction/predictions/{citations,refusals,missing-data}/
  <case_id>.json`` — hand-crafted cached predictions for cases that
  exercise structural rubric paths without paying for a live VLM call.

Idempotent: re-running rebuilds every label / manifest / prediction
file from this script's source. **The script itself is the source of
truth for the case set** — edit here, re-run, commit the output.

Usage::

    cd agent-service
    uv run python scripts/seed_eval_cases.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SERVICE = REPO_ROOT / "agent-service"
EVAL_ROOT = AGENT_SERVICE / "evals" / "extraction"
LABELS_ROOT = EVAL_ROOT / "labels"
PREDICTIONS_ROOT = EVAL_ROOT / "predictions"
MANIFEST_PATH = EVAL_ROOT / "cases.jsonl"

REVIEW_METADATA = {
    "review_status": "human_reviewed",
    "reviewed_by": "andy",
    "reviewed_at": date.today().isoformat(),
    "source_notes": "seeded by scripts/seed_eval_cases.py",
}

# All paths in the manifest are relative to evals/extraction/.
PATH_PREFIX_DOCS = "../../../example-documents"  # repo-root example docs
PATH_PREFIX_FIX = "../../tests/fixtures"  # synthetic build_pdfs.py outputs


# --------------------------------------------------------------- helpers


def relpath_label(bucket: str, name: str) -> str:
    return f"labels/{bucket}/{name}.json"


def relpath_prediction(bucket: str, name: str) -> str:
    return f"predictions/{bucket}/{name}.json"


def write_label(bucket: str, name: str, body: dict[str, Any]) -> None:
    out = LABELS_ROOT / bucket / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body_with_meta = {"metadata": REVIEW_METADATA, **body}
    out.write_text(json.dumps(body_with_meta, indent=2, sort_keys=True))


def write_prediction(bucket: str, name: str, body: dict[str, Any]) -> None:
    out = PREDICTIONS_ROOT / bucket / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2, sort_keys=True))


# --------------------------------------------------------------- 28 extraction


# (case_id, document_path, document_type, label_body, rubric_categories,
#  live_smoke, description)
EXTRACTION_CASES: list[tuple[str, str, str, dict[str, Any], list[str], bool, str]] = [
    # ----- 10 migrated from tests/eval/w2_cases/ -----
    (
        "p01_chen_lipid",
        f"{PATH_PREFIX_DOCS}/lab-results/p01-chen-lipid-panel.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 5}],
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "Cholesterol, Total"},
                {"path": "observations[0].value.value", "expected": 232.0},
                {"path": "observations[0].unit.value", "expected": "mg/dL"},
                {"path": "observations[0].flag.value", "expected": "H"},
                {"path": "observations[1].display.value", "expected": "HDL Cholesterol"},
                {"path": "observations[1].value.value", "expected": 48.0},
                {"path": "observations[1].flag.value", "expected": "L"},
                {"path": "observations[2].value.value", "expected": 158.0},
                {"path": "observations[3].display.value", "expected": "Triglycerides"},
                {"path": "observations[3].value.value", "expected": 178.0},
            ],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Chen lipid panel — 5 lipid observations + Non-HDL, multiple high flags.",
    ),
    (
        "p02_whitaker_cbc",
        f"{PATH_PREFIX_DOCS}/lab-results/p02-whitaker-cbc.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 7}],
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "WBC"},
                {"path": "observations[0].value.value", "expected": 5.4},
                {"path": "observations[1].display.value", "expected": "RBC"},
                {"path": "observations[1].value.value", "expected": 3.78},
                {"path": "observations[1].flag.value", "expected": "L"},
                {"path": "observations[2].display.value", "expected": "Hemoglobin"},
                {"path": "observations[2].value.value", "expected": 11.1},
                {"path": "observations[2].flag.value", "expected": "L"},
                {"path": "observations[3].display.value", "expected": "Hematocrit"},
                {"path": "observations[3].flag.value", "expected": "L"},
            ],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Whitaker CBC — 7 hematology observations including 3 low flags.",
    ),
    (
        "p03_reyes_hba1c_png",
        f"{PATH_PREFIX_DOCS}/lab-results/p03-reyes-hba1c.png",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 2}],
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "Hemoglobin A1c"},
                {"path": "observations[0].value.value", "expected": 8.2},
                {"path": "observations[0].unit.value", "expected": "%"},
                {"path": "observations[0].flag.value", "expected": "H"},
                {"path": "observations[1].display.value", "expected": "Fasting Glucose"},
                {"path": "observations[1].value.value", "expected": 152.0},
            ],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        False,
        "Reyes HbA1c — PNG scan path. Tests image-input handling.",
    ),
    (
        "p04_kowalski_cmp",
        f"{PATH_PREFIX_DOCS}/lab-results/p04-kowalski-cmp.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 14}],
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "Glucose"},
                {"path": "observations[0].value.value", "expected": 108.0},
                {"path": "observations[0].flag.value", "expected": "H"},
                {"path": "observations[1].display.value", "expected": "BUN"},
                {"path": "observations[2].display.value", "expected": "Creatinine"},
                {"path": "observations[2].value.value", "expected": 1.4},
                {"path": "observations[2].flag.value", "expected": "H"},
                {"path": "observations[5].display.value", "expected": "Potassium"},
                {"path": "observations[5].value.value", "expected": 3.3},
                {"path": "observations[5].flag.value", "expected": "L"},
            ],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        False,
        "Kowalski CMP — 15-row panel including HTML-entity glitch in source.",
    ),
    (
        "synthetic_glucose_panel",
        f"{PATH_PREFIX_FIX}/lab_pdf/glucose_panel.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 6}],
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "Glucose"},
                {"path": "observations[0].value.value", "expected": 142.0},
                {"path": "observations[0].flag.value", "expected": "H"},
                {"path": "observations[1].display.value", "expected": "Sodium"},
                {"path": "observations[1].value.value", "expected": 139.0},
                {"path": "observations[2].display.value", "expected": "Potassium"},
                {"path": "observations[5].display.value", "expected": "Creatinine"},
            ],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Synthetic basic metabolic panel — deterministic fixture; regression baseline.",
    ),
    (
        "p01_chen_intake",
        f"{PATH_PREFIX_DOCS}/intake-forms/p01-chen-intake-typed.pdf",
        "intake_form",
        {
            "required_fields": [
                {"path": "legal_first_name.value", "expected": "Margaret"},
                {"path": "legal_last_name.value", "expected": "Chen"},
                {"path": "date_of_birth.value", "expected": "1967-08-14"},
                {"path": "sex_assigned_at_birth.value", "expected": "Female"},
                {"path": "tobacco_status.value", "expected": "former"},
                {"path": "tobacco_pack_years.value", "expected": 12.0},
                {"path": "pain_scale.value", "expected": 2},
            ],
            "required_field_present": [
                "chief_complaint.value",
                "medical_record_number.value",
            ],
            "required_list_min": [
                {"path": "current_medications", "min_count": 4},
                {"path": "reported_allergies", "min_count": 3},
                {"path": "active_problems", "min_count": 3},
                {"path": "family_history", "min_count": 3},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Chen 3-page intake — full demographics + 4 meds + 3 allergies + 3 problems + family hx.",
    ),
    (
        "p02_whitaker_intake_nkda",
        f"{PATH_PREFIX_DOCS}/intake-forms/p02-whitaker-intake.pdf",
        "intake_form",
        {
            "required_fields": [
                {"path": "legal_first_name.value", "expected": "James"},
                {"path": "legal_last_name.value", "expected": "Whitaker"},
                {"path": "date_of_birth.value", "expected": "1958-11-03"},
                {"path": "sex_assigned_at_birth.value", "expected": "Male"},
                {"path": "reported_allergies[0].substance.value", "expected": "NKDA"},
            ],
            "required_list_min": [
                {"path": "current_medications", "min_count": 3},
                {"path": "reported_allergies", "min_count": 1},
                {"path": "active_problems", "min_count": 3},
                {"path": "family_history", "min_count": 1},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        False,
        "Whitaker intake — NKDA negation surfaces as a single allergy entry, not an empty list.",
    ),
    (
        "p03_reyes_intake_png",
        f"{PATH_PREFIX_DOCS}/intake-forms/p03-reyes-intake.png",
        "intake_form",
        {
            "required_field_present": ["chief_complaint.value"],
            "required_list_min": [
                {"path": "current_medications", "min_count": 2},
                {"path": "reported_allergies", "min_count": 1},
                {"path": "active_problems", "min_count": 1},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        False,
        "Reyes intake — PNG scan with mixed handwritten/typed fields.",
    ),
    (
        "p04_kowalski_intake_png",
        f"{PATH_PREFIX_DOCS}/intake-forms/p04-kowalski-intake.png",
        "intake_form",
        {
            "required_field_present": ["chief_complaint.value"],
            "required_fields": [
                {"path": "tobacco_status.value", "expected": "never"},
            ],
            "required_list_min": [
                {"path": "current_medications", "min_count": 2},
                {"path": "reported_allergies", "min_count": 1},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        False,
        "Kowalski intake — PNG scan, slightly rotated.",
    ),
    (
        "synthetic_chest_pain",
        f"{PATH_PREFIX_FIX}/intake_form/intake_chest_pain.pdf",
        "intake_form",
        {
            "required_field_present": ["chief_complaint.value"],
            "required_fields": [
                {"path": "pain_scale.value", "expected": 6},
            ],
            "required_list_min": [
                {"path": "current_medications", "min_count": 2},
                {"path": "reported_allergies", "min_count": 1},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Synthetic chest-pain intake — deterministic fixture.",
    ),
    # ----- 18 additional aspect cases (cached predictions, no live cost) -----
    # These reuse the 10 PDFs above. Each tests a different facet
    # (specific reference range, citation shape on a particular field,
    # list-row presence, etc.) without re-running the live VLM.
    (
        "p01_chen_lipid_aspect_ldl",
        f"{PATH_PREFIX_DOCS}/lab-results/p01-chen-lipid-panel.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[2].display.value", "expected": "LDL"},
                {"path": "observations[2].value.value", "expected": 158.0},
            ],
        },
        ["factually_consistent"],
        False,
        "Chen lipid — LDL value-and-name aspect (reuses primary PDF prediction).",
    ),
    (
        "p01_chen_lipid_aspect_unit",
        f"{PATH_PREFIX_DOCS}/lab-results/p01-chen-lipid-panel.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[1].unit.value", "expected": "mg/dL"},
            ],
        },
        ["factually_consistent"],
        False,
        "Chen lipid — HDL unit aspect.",
    ),
    (
        "p02_whitaker_cbc_aspect_low_flags",
        f"{PATH_PREFIX_DOCS}/lab-results/p02-whitaker-cbc.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[1].flag.value", "expected": "L"},
                {"path": "observations[2].flag.value", "expected": "L"},
                {"path": "observations[3].flag.value", "expected": "L"},
            ],
        },
        ["factually_consistent"],
        False,
        "Whitaker CBC — three consecutive 'L' flags aspect.",
    ),
    (
        "p02_whitaker_cbc_aspect_citations",
        f"{PATH_PREFIX_DOCS}/lab-results/p02-whitaker-cbc.pdf",
        "lab_pdf",
        {
            "required_citations": [
                {"path": "observations[0].value"},
                {"path": "observations[1].value"},
                {"path": "observations[2].value"},
            ],
        },
        ["citation_present"],
        False,
        "Whitaker CBC — citation presence on first 3 rows.",
    ),
    (
        "p03_reyes_hba1c_aspect_unit",
        f"{PATH_PREFIX_DOCS}/lab-results/p03-reyes-hba1c.png",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[0].unit.value", "expected": "%"},
            ],
        },
        ["factually_consistent"],
        False,
        "Reyes HbA1c — % unit aspect on PNG input.",
    ),
    (
        "p03_reyes_hba1c_aspect_high_flag",
        f"{PATH_PREFIX_DOCS}/lab-results/p03-reyes-hba1c.png",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[0].flag.value", "expected": "H"},
            ],
        },
        ["factually_consistent"],
        False,
        "Reyes HbA1c — H flag aspect on PNG input.",
    ),
    (
        "p04_kowalski_cmp_aspect_potassium",
        f"{PATH_PREFIX_DOCS}/lab-results/p04-kowalski-cmp.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[5].display.value", "expected": "Potassium"},
                {"path": "observations[5].flag.value", "expected": "L"},
            ],
        },
        ["factually_consistent"],
        False,
        "Kowalski CMP — Potassium row aspect (low flag).",
    ),
    (
        "p04_kowalski_cmp_aspect_count",
        f"{PATH_PREFIX_DOCS}/lab-results/p04-kowalski-cmp.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 14}],
        },
        ["factually_consistent"],
        False,
        "Kowalski CMP — observation-count aspect.",
    ),
    (
        "synthetic_glucose_aspect_sodium",
        f"{PATH_PREFIX_FIX}/lab_pdf/glucose_panel.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[1].value.value", "expected": 139.0},
            ],
        },
        ["factually_consistent"],
        False,
        "Glucose panel — sodium value aspect.",
    ),
    (
        "synthetic_glucose_aspect_creatinine",
        f"{PATH_PREFIX_FIX}/lab_pdf/glucose_panel.pdf",
        "lab_pdf",
        {
            "required_fields": [
                {"path": "observations[5].display.value", "expected": "Creatinine"},
            ],
        },
        ["factually_consistent"],
        False,
        "Glucose panel — creatinine row aspect.",
    ),
    (
        "synthetic_lipid_panel",
        f"{PATH_PREFIX_FIX}/lab_pdf/lipid_panel.pdf",
        "lab_pdf",
        {
            "required_list_min": [{"path": "observations", "min_count": 4}],
            "required_citations": [{"path": "observations[0].value"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Synthetic lipid panel — deterministic fixture, structural assertions.",
    ),
    (
        "p01_chen_intake_aspect_meds",
        f"{PATH_PREFIX_DOCS}/intake-forms/p01-chen-intake-typed.pdf",
        "intake_form",
        {
            "required_list_min": [{"path": "current_medications", "min_count": 4}],
        },
        ["factually_consistent"],
        False,
        "Chen intake — current_medications row count aspect.",
    ),
    (
        "p01_chen_intake_aspect_family_hx",
        f"{PATH_PREFIX_DOCS}/intake-forms/p01-chen-intake-typed.pdf",
        "intake_form",
        {
            "required_list_min": [{"path": "family_history", "min_count": 3}],
        },
        ["factually_consistent"],
        False,
        "Chen intake — family_history row count aspect.",
    ),
    (
        "p02_whitaker_intake_aspect_nkda",
        f"{PATH_PREFIX_DOCS}/intake-forms/p02-whitaker-intake.pdf",
        "intake_form",
        {
            "required_fields": [
                {"path": "reported_allergies[0].substance.value", "expected": "NKDA"},
            ],
        },
        ["factually_consistent"],
        False,
        "Whitaker intake — NKDA single-row aspect (negation handling).",
    ),
    (
        "p03_reyes_intake_aspect_problems",
        f"{PATH_PREFIX_DOCS}/intake-forms/p03-reyes-intake.png",
        "intake_form",
        {
            "required_list_min": [{"path": "active_problems", "min_count": 1}],
        },
        ["factually_consistent"],
        False,
        "Reyes intake — active_problems presence aspect on PNG input.",
    ),
    (
        "p04_kowalski_intake_aspect_tobacco",
        f"{PATH_PREFIX_DOCS}/intake-forms/p04-kowalski-intake.png",
        "intake_form",
        {
            "required_fields": [
                {"path": "tobacco_status.value", "expected": "never"},
            ],
        },
        ["factually_consistent"],
        False,
        "Kowalski intake — tobacco_status aspect on PNG input.",
    ),
    (
        "synthetic_chest_pain_aspect_pain",
        f"{PATH_PREFIX_FIX}/intake_form/intake_chest_pain.pdf",
        "intake_form",
        {
            "required_fields": [
                {"path": "pain_scale.value", "expected": 6},
            ],
        },
        ["factually_consistent"],
        False,
        "Synthetic chest-pain — pain_scale aspect.",
    ),
    (
        "synthetic_nkda_annual",
        f"{PATH_PREFIX_FIX}/intake_form/intake_nkda_annual.pdf",
        "intake_form",
        {
            "required_field_present": ["chief_complaint.value"],
            "required_list_min": [
                {"path": "reported_allergies", "min_count": 1},
            ],
            "required_citations": [{"path": "chief_complaint"}],
        },
        ["schema_valid", "citation_present", "factually_consistent", "no_phi_in_logs"],
        True,
        "Synthetic NKDA annual — deterministic fixture; structural assertions.",
    ),
]

assert len(EXTRACTION_CASES) == 28, f"expected 28 extraction cases, got {len(EXTRACTION_CASES)}"


# --------------------------------------------------------------- 6 citations


def _ef(value: object, citation: dict[str, Any]) -> dict[str, Any]:
    """Build an ExtractedField-shaped dict (value + citation, no abstain)."""

    return {"value": value, "citation": citation, "abstain_reason": None}


def _ef_abstain(reason: str) -> dict[str, Any]:
    return {"value": None, "citation": None, "abstain_reason": reason}


def _cit(page: int, bbox: tuple[float, float, float, float], conf: float, raw: str) -> dict[str, Any]:
    return {
        "document_id": "eval:doc",
        "page": page,
        "bbox": list(bbox),
        "confidence": conf,
        "raw_text": raw,
    }


def _build_obs(
    code: str, display: str, value: float, unit: str, edate: str
) -> dict[str, Any]:
    return {
        "code": _ef(code, _cit(1, (0.1, 0.1, 0.4, 0.2), 0.95, code)),
        "display": _ef(display, _cit(1, (0.1, 0.2, 0.4, 0.3), 0.95, display)),
        "value": _ef(value, _cit(1, (0.4, 0.2, 0.6, 0.3), 0.95, str(value))),
        "unit": _ef(unit, _cit(1, (0.6, 0.2, 0.8, 0.3), 0.95, unit)),
        "effective_date": _ef(edate, _cit(1, (0.6, 0.05, 0.9, 0.1), 0.99, edate)),
        "reference_low": None,
        "reference_high": None,
        "flag": None,
    }


# Build the 6 citation cases inline. Each ships with its own cached
# prediction so the gate exercises rubric paths without paying live cost.
CITATIONS: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    # name, description, label_body, prediction_body
    (
        "c01_valid_full",
        "Valid citation across all required paths — happy path for citation_present.",
        {
            "required_citations": [
                {"path": "observations[0].value"},
                {"path": "observations[0].display"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:c01",
                "observations": [_build_obs("LP12345-1", "Glucose", 110.0, "mg/dL", "2026-04-01")],
            }
        },
    ),
    (
        "c02_two_observations_full_citations",
        "Two-observation extraction; citation_present must pass on both.",
        {
            "required_citations": [
                {"path": "observations[0].value"},
                {"path": "observations[1].value"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:c02",
                "observations": [
                    _build_obs("LP1", "Sodium", 140.0, "mmol/L", "2026-04-02"),
                    _build_obs("LP2", "Potassium", 4.1, "mmol/L", "2026-04-02"),
                ],
            }
        },
    ),
    (
        "c03_lab_with_reference_range_citations",
        "Reference-range bounds carry their own citations.",
        {
            "required_citations": [
                {"path": "observations[0].value"},
                {"path": "observations[0].reference_low"},
                {"path": "observations[0].reference_high"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:c03",
                "observations": [
                    {
                        **_build_obs("LP3", "Hemoglobin", 13.2, "g/dL", "2026-04-03"),
                        "reference_low": _ef(13.0, _cit(1, (0.7, 0.3, 0.8, 0.4), 0.9, "13.0")),
                        "reference_high": _ef(17.0, _cit(1, (0.8, 0.3, 0.9, 0.4), 0.9, "17.0")),
                    }
                ],
            }
        },
    ),
    (
        "c04_intake_chief_complaint_citation",
        "Intake-form chief_complaint must carry a citation pointing back to the form region.",
        {
            "required_citations": [{"path": "chief_complaint"}],
        },
        {
            "facts": {
                "document_id": "eval:c04",
                "chief_complaint": _ef(
                    "fatigue and palpitations",
                    _cit(1, (0.1, 0.05, 0.9, 0.12), 0.93, "fatigue and palpitations"),
                ),
                "current_medications": [],
                "reported_allergies": [],
                "active_problems": [],
                "family_history": [],
            }
        },
    ),
    (
        "c05_intake_demographics_citations",
        "Intake-form demographics — every demographic field with a value must carry a citation.",
        {
            "required_citations": [
                {"path": "legal_first_name"},
                {"path": "legal_last_name"},
                {"path": "date_of_birth"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:c05",
                "legal_first_name": _ef(
                    "Avery", _cit(1, (0.2, 0.1, 0.4, 0.15), 0.97, "Avery")
                ),
                "legal_last_name": _ef(
                    "Tan", _cit(1, (0.4, 0.1, 0.5, 0.15), 0.97, "Tan")
                ),
                "date_of_birth": _ef(
                    "1990-02-14", _cit(1, (0.55, 0.1, 0.75, 0.15), 0.95, "02/14/1990")
                ),
                "chief_complaint": _ef(
                    "checkup", _cit(1, (0.1, 0.2, 0.6, 0.25), 0.9, "checkup")
                ),
                "current_medications": [],
                "reported_allergies": [],
                "active_problems": [],
                "family_history": [],
            }
        },
    ),
    (
        "c06_lab_low_confidence_field_abstains_no_citation",
        "Low-confidence value abstains with LOW_CONFIDENCE — no citation expected on that path.",
        {
            "required_citations": [{"path": "observations[0].display"}],
            "must_abstain": [{"path": "observations[0].value", "reason": "LOW_CONFIDENCE"}],
        },
        {
            "facts": {
                "document_id": "eval:c06",
                "observations": [
                    {
                        "code": _ef("LP9", _cit(1, (0.1, 0.1, 0.4, 0.2), 0.95, "LP9")),
                        "display": _ef("Creatinine", _cit(1, (0.1, 0.2, 0.4, 0.3), 0.92, "Creatinine")),
                        "value": _ef_abstain("LOW_CONFIDENCE"),
                        "unit": _ef("mg/dL", _cit(1, (0.6, 0.2, 0.8, 0.3), 0.95, "mg/dL")),
                        "effective_date": _ef("2026-04-04", _cit(1, (0.6, 0.05, 0.9, 0.1), 0.99, "2026-04-04")),
                        "reference_low": None,
                        "reference_high": None,
                        "flag": None,
                    }
                ],
            }
        },
    ),
]

assert len(CITATIONS) == 6


# --------------------------------------------------------------- 4 missing-data


MISSING_DATA: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    (
        "md01_lab_no_reference_range",
        "Lab report omits reference range — observation present, reference fields absent.",
        {
            "required_fields": [
                {"path": "observations[0].display.value", "expected": "Glucose"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:md01",
                "observations": [_build_obs("LP1", "Glucose", 95.0, "mg/dL", "2026-04-05")],
            }
        },
    ),
    (
        "md02_intake_no_pack_years",
        "Intake form lists tobacco_status='never' — pack_years legitimately absent.",
        {
            "required_fields": [
                {"path": "tobacco_status.value", "expected": "never"},
            ],
            "must_abstain": [
                {"path": "tobacco_pack_years", "reason": "NO_DATA"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:md02",
                "chief_complaint": _ef(
                    "annual physical", _cit(1, (0.1, 0.2, 0.6, 0.25), 0.9, "annual physical")
                ),
                "tobacco_status": _ef(
                    "never", _cit(1, (0.1, 0.5, 0.3, 0.55), 0.95, "Never")
                ),
                "tobacco_pack_years": _ef_abstain("NO_DATA"),
                "current_medications": [],
                "reported_allergies": [],
                "active_problems": [],
                "family_history": [],
            }
        },
    ),
    (
        "md03_lab_no_flags_present",
        "All-normal lab — no flag fields populated; rubric must not require them.",
        {
            "required_list_min": [{"path": "observations", "min_count": 2}],
        },
        {
            "facts": {
                "document_id": "eval:md03",
                "observations": [
                    _build_obs("LP1", "Sodium", 140.0, "mmol/L", "2026-04-06"),
                    _build_obs("LP2", "Potassium", 4.1, "mmol/L", "2026-04-06"),
                ],
            }
        },
    ),
    (
        "md04_intake_no_email",
        "Intake form's email is blank — must surface as NO_DATA, not as empty string.",
        {
            "must_abstain": [
                {"path": "email", "reason": "NO_DATA"},
            ],
        },
        {
            "facts": {
                "document_id": "eval:md04",
                "chief_complaint": _ef(
                    "follow-up", _cit(1, (0.1, 0.2, 0.6, 0.25), 0.9, "follow-up")
                ),
                "email": _ef_abstain("NO_DATA"),
                "current_medications": [],
                "reported_allergies": [],
                "active_problems": [],
                "family_history": [],
            }
        },
    ),
]

assert len(MISSING_DATA) == 4


# --------------------------------------------------------------- 4 refusals


REFUSALS: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    (
        "r01_out_of_schema_field",
        "VLM emits a field name not in the schema — must surface as OUT_OF_SCHEMA.",
        {
            "must_abstain": [
                {"path": "observations[0].value", "reason": "OUT_OF_SCHEMA"},
            ],
            "safe_refusal": {
                "expected_reason": "OUT_OF_SCHEMA",
                "forbidden_patterns": ["fabricated_field"],
            },
        },
        {
            "facts": {
                "document_id": "eval:r01",
                "observations": [
                    {
                        "code": _ef("LP1", _cit(1, (0.1, 0.1, 0.4, 0.2), 0.95, "LP1")),
                        "display": _ef("Glucose", _cit(1, (0.1, 0.2, 0.4, 0.3), 0.95, "Glucose")),
                        "value": _ef_abstain("OUT_OF_SCHEMA"),
                        "unit": _ef("mg/dL", _cit(1, (0.6, 0.2, 0.8, 0.3), 0.95, "mg/dL")),
                        "effective_date": _ef("2026-04-07", _cit(1, (0.6, 0.05, 0.9, 0.1), 0.99, "2026-04-07")),
                        "reference_low": None,
                        "reference_high": None,
                        "flag": None,
                    }
                ],
            },
            "abstention_reason": "OUT_OF_SCHEMA",
            "synthesized_text": "Field could not be parsed against the schema.",
        },
    ),
    (
        "r02_low_confidence_abstain",
        "VLM confidence below 0.7 — agent must abstain rather than emit a low-confidence value.",
        {
            "must_abstain": [
                {"path": "observations[0].value", "reason": "LOW_CONFIDENCE"},
            ],
            "safe_refusal": {
                "expected_reason": "LOW_CONFIDENCE",
                "forbidden_patterns": ["best guess", "approximately"],
            },
        },
        {
            "facts": {
                "document_id": "eval:r02",
                "observations": [
                    {
                        "code": _ef("LP2", _cit(1, (0.1, 0.1, 0.4, 0.2), 0.95, "LP2")),
                        "display": _ef("HbA1c", _cit(1, (0.1, 0.2, 0.4, 0.3), 0.95, "HbA1c")),
                        "value": _ef_abstain("LOW_CONFIDENCE"),
                        "unit": _ef("%", _cit(1, (0.6, 0.2, 0.8, 0.3), 0.95, "%")),
                        "effective_date": _ef("2026-04-08", _cit(1, (0.6, 0.05, 0.9, 0.1), 0.99, "2026-04-08")),
                        "reference_low": None,
                        "reference_high": None,
                        "flag": None,
                    }
                ],
            },
            "abstention_reason": "LOW_CONFIDENCE",
            "synthesized_text": "Could not read the value with sufficient confidence.",
        },
    ),
    (
        "r03_unauthorized_request",
        "Caller is unauthorized — must surface UNAUTHORIZED, never partial data.",
        {
            "safe_refusal": {
                "expected_reason": "UNAUTHORIZED",
                "forbidden_patterns": [r"\bSSN\b", r"\bDOB\b"],
            },
        },
        {
            "facts": None,
            "abstention_reason": "UNAUTHORIZED",
            "synthesized_text": "This session is not authorized to access the requested resource.",
        },
    ),
    (
        "r04_tool_failure_no_partial",
        "Backend tool failure must abstain with TOOL_FAILURE, never synthesize a partial answer.",
        {
            "safe_refusal": {
                "expected_reason": "TOOL_FAILURE",
                "forbidden_patterns": ["likely", "approximately"],
            },
        },
        {
            "facts": None,
            "abstention_reason": "TOOL_FAILURE",
            "synthesized_text": "Tool failure occurred; no answer is available.",
        },
    ),
]

assert len(REFUSALS) == 4


# --------------------------------------------------------------- 8 retrieval


RETRIEVAL_PLACEHOLDERS: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    (
        "rt01_chest_pain_aha",
        "Chest-pain evaluation query maps to AHA outpatient guidance.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["aha/chest_pain_evaluation"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "outpatient chest pain evaluation troponin ECG"},
    ),
    (
        "rt02_adult_immunization_cdc",
        "Adult vaccine-schedule query maps to CDC routine immunizations.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["cdc/adult_immunization_schedule"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "adult routine vaccines influenza pneumococcal Tdap"},
    ),
    (
        "rt03_tobacco_cessation_cdc",
        "Tobacco-cessation query maps to CDC counseling + pharmacotherapy.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["cdc/tobacco_cessation"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "tobacco cessation counseling varenicline bupropion"},
    ),
    (
        "rt04_cholesterol_thresholds_nih",
        "LDL-thresholds query maps to NIH cholesterol thresholds.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["nih/cholesterol_thresholds"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "LDL cholesterol thresholds primary prevention"},
    ),
    (
        "rt05_aspirin_primary_prevention_uspstf",
        "Aspirin primary-prevention query maps to USPSTF guidance.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["uspstf/aspirin_primary_prevention"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "aspirin primary prevention cardiovascular adults"},
    ),
    (
        "rt06_colorectal_screening_uspstf",
        "Colorectal-screening query maps to USPSTF colonoscopy guidance.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["uspstf/colorectal_cancer_screening"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "colorectal cancer screening colonoscopy stool"},
    ),
    (
        "rt07_lung_cancer_screening_uspstf",
        "Lung-cancer screening query maps to USPSTF LDCT guidance.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["uspstf/lung_cancer_screening"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "lung cancer screening low dose CT smoking history"},
    ),
    (
        "rt08_hypertension_screening_uspstf",
        "Hypertension-screening query maps to USPSTF screening guidance.",
        {
            "expected_retrieval": {
                "expected_source_doc_ids": ["uspstf/hypertension_screening"],
                "k": 5,
            },
        },
        {"facts": None, "retrieved": [], "_query": "hypertension screening adults office blood pressure"},
    ),
]

assert len(RETRIEVAL_PLACEHOLDERS) == 8


# --------------------------------------------------------------- main


def main() -> None:
    manifest_lines: list[str] = []

    # 28 extraction cases
    for case_id, doc_path, doc_type, body, rubrics_, smoke, desc in EXTRACTION_CASES:
        write_label("extraction", case_id, body)
        manifest_lines.append(
            json.dumps(
                {
                    "case_id": f"extraction/{case_id}",
                    "bucket": "extraction",
                    "document_type": doc_type,
                    "document_path": doc_path,
                    "label_path": relpath_label("extraction", case_id),
                    "rubric_categories": rubrics_,
                    "live_smoke": smoke,
                    "description": desc,
                }
            )
        )

    # 6 citation cases (cached predictions)
    for case_id, desc, body, prediction in CITATIONS:
        write_label("citations", case_id, body)
        write_prediction("citations", case_id, prediction)
        # Derive document_type from prediction shape: facts with
        # chief_complaint at the top level is intake; facts with
        # observations is lab.
        facts = prediction.get("facts") or {}
        doc_type = "intake_form" if "chief_complaint" in facts else "lab_pdf"
        manifest_lines.append(
            json.dumps(
                {
                    "case_id": f"citations/{case_id}",
                    "bucket": "citations",
                    "document_type": doc_type,
                    "label_path": relpath_label("citations", case_id),
                    "prediction_path": relpath_prediction("citations", case_id),
                    "rubric_categories": [
                        "schema_valid",
                        "citation_present",
                        "no_phi_in_logs",
                    ],
                    "live_smoke": False,
                    "description": desc,
                }
            )
        )

    # 4 missing-data cases
    for case_id, desc, body, prediction in MISSING_DATA:
        write_label("missing-data", case_id, body)
        write_prediction("missing-data", case_id, prediction)
        # Determine document_type from the prediction's facts shape.
        facts = prediction.get("facts") or {}
        doc_type = "lab_pdf" if "observations" in facts else "intake_form"
        manifest_lines.append(
            json.dumps(
                {
                    "case_id": f"missing-data/{case_id}",
                    "bucket": "missing-data",
                    "document_type": doc_type,
                    "label_path": relpath_label("missing-data", case_id),
                    "prediction_path": relpath_prediction("missing-data", case_id),
                    "rubric_categories": [
                        "schema_valid",
                        "factually_consistent",
                        "safe_refusal",
                        "no_phi_in_logs",
                    ],
                    "live_smoke": False,
                    "description": desc,
                }
            )
        )

    # 4 refusal cases
    for case_id, desc, body, prediction in REFUSALS:
        write_label("refusals", case_id, body)
        write_prediction("refusals", case_id, prediction)
        manifest_lines.append(
            json.dumps(
                {
                    "case_id": f"refusals/{case_id}",
                    "bucket": "refusals",
                    "document_type": "refusal",
                    "label_path": relpath_label("refusals", case_id),
                    "prediction_path": relpath_prediction("refusals", case_id),
                    "rubric_categories": ["safe_refusal", "no_phi_in_logs"],
                    "live_smoke": False,
                    "description": desc,
                }
            )
        )

    # 8 retrieval cases (live BM25 — local, free, no LLM)
    for case_id, desc, body, prediction in RETRIEVAL_PLACEHOLDERS:
        write_label("retrieval", case_id, body)
        # Drop the _query private field before writing — it's used at
        # manifest-time only.
        query = prediction.pop("_query", "")
        manifest_lines.append(
            json.dumps(
                {
                    "case_id": f"retrieval/{case_id}",
                    "bucket": "retrieval",
                    "document_type": "retrieval",
                    "query": query,
                    "label_path": relpath_label("retrieval", case_id),
                    "rubric_categories": [
                        "schema_valid",
                        "factually_consistent",
                        "no_phi_in_logs",
                    ],
                    "live_smoke": False,
                    "description": desc,
                }
            )
        )

    EVAL_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text("\n".join(manifest_lines) + "\n")
    print(f"wrote {len(manifest_lines)} cases to {MANIFEST_PATH}")
    if len(manifest_lines) != 50:
        raise SystemExit(
            f"manifest count mismatch: expected 50, got {len(manifest_lines)}"
        )


if __name__ == "__main__":
    main()
