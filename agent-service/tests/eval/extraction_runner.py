"""CLI eval runner for the Week 2 extraction buckets.

Loads case JSON files from ``tests/eval/cases/extraction-lab/`` and
``tests/eval/cases/extraction-intake/``, runs each case's
``document_path`` through the live extractor, and evaluates boolean
rubrics against the resulting ``LabPdfFacts`` / ``IntakeFormFacts``.

Boolean rubrics — each one returns pass/fail, no scalar scoring:

* ``observation_count_min`` — the extraction has at least N rows in
  ``observations`` (lab) or in any list field (intake).
* ``field_equals`` — a field path resolves to the expected value.
  Path syntax: ``observations[0].value.value`` (dotted, indices in
  brackets). Matches the JSON structure of ``model_dump()``.
* ``field_present`` — a field path resolves to a non-null value.
* ``field_abstains`` — a field path's ``abstain_reason`` matches the
  expected ``RuntimeAbstainReason`` value.
* ``list_min`` — a list-typed field has at least N entries.

Output:
* prints per-case pass/fail to stdout
* exits non-zero on any case failure (CI gate)
* with ``--csv-out PATH``, writes a flat results CSV (one row per
  rubric assertion across all cases) for the human-verification step
  of the rubric submission.

Usage::

    cd agent-service
    uv run python -m tests.eval.extraction_runner
    uv run python -m tests.eval.extraction_runner --csv-out results.csv
    uv run python -m tests.eval.extraction_runner --bucket extraction-lab

Reads ``ANTHROPIC_API_KEY`` from the environment / ``.env``. Each case
costs roughly $0.05–$0.10 in vision API; full bucket of 10 ≈ $0.80.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from clinical_copilot.config import get_settings
from clinical_copilot.documents.extractor import (
    DocumentType,
    ExtractorError,
    UnsupportedDocumentTypeError,
    extract,
)

CASES_ROOT = Path(__file__).resolve().parent / "w2_cases"
DEFAULT_BUCKETS = (
    "extraction-lab",
    "extraction-intake",
    "extraction-referral",
    "extraction-fax",
    "extraction-workbook",
    "extraction-hl7-oru",
    "extraction-hl7-adt",
)

_INDEX_RE = re.compile(r"^(?P<name>[A-Za-z_]\w*)(?:\[(?P<idx>\d+)\])?$")


@dataclass(slots=True)
class RubricResult:
    case_id: str
    bucket: str
    rubric_name: str
    target: str
    expected: object
    observed: object
    passed: bool
    note: str = ""


@dataclass(slots=True)
class CaseResult:
    case_id: str
    bucket: str
    rubrics: list[RubricResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.rubrics)


def resolve_path(facts: object, path: str) -> object:
    """Walk ``path`` through ``facts`` and return the final value.

    Returns ``None`` for missing keys / out-of-range indices so the
    rubric layer can detect "field absent" without an exception.
    """

    cursor: object = facts
    for raw in path.split("."):
        m = _INDEX_RE.match(raw)
        if m is None:
            return None
        name = m.group("name")
        idx = m.group("idx")
        if isinstance(cursor, dict):
            cursor = cursor.get(name)
        else:
            return None
        if idx is not None:
            if not isinstance(cursor, list):
                return None
            i = int(idx)
            if i < 0 or i >= len(cursor):
                return None
            cursor = cursor[i]
    return cursor


def evaluate_case(case: dict[str, Any], facts: dict[str, Any]) -> list[RubricResult]:
    """Run every rubric in ``case['expectations']`` against ``facts``."""

    case_id = str(case["case_id"])
    bucket = str(case.get("bucket", ""))
    expectations = case.get("expectations") or {}
    out: list[RubricResult] = []

    # observation_count_min — only meaningful for lab cases
    if "observation_count_min" in expectations:
        expected_min = int(expectations["observation_count_min"])
        observations = facts.get("observations")
        obs_count = len(observations) if isinstance(observations, list) else 0
        out.append(
            RubricResult(
                case_id=case_id,
                bucket=bucket,
                rubric_name="observation_count_min",
                target="observations",
                expected=expected_min,
                observed=obs_count,
                passed=obs_count >= expected_min,
            )
        )

    # list_min — same shape, generic over any list-typed field
    for path, raw_min in (expectations.get("list_min") or {}).items():
        list_value = resolve_path(facts, path)
        list_count = len(list_value) if isinstance(list_value, list) else 0
        min_threshold = int(raw_min)
        out.append(
            RubricResult(
                case_id=case_id,
                bucket=bucket,
                rubric_name="list_min",
                target=path,
                expected=min_threshold,
                observed=list_count,
                passed=list_count >= min_threshold,
            )
        )

    # field_equals — exact match (case-insensitive for strings)
    for path, expected_value in (expectations.get("field_equals") or {}).items():
        eq_observed = resolve_path(facts, path)
        out.append(
            RubricResult(
                case_id=case_id,
                bucket=bucket,
                rubric_name="field_equals",
                target=path,
                expected=expected_value,
                observed=eq_observed,
                passed=_values_equal(expected_value, eq_observed),
            )
        )

    # field_present — non-null
    for path in expectations.get("field_present") or []:
        present_observed = resolve_path(facts, path)
        out.append(
            RubricResult(
                case_id=case_id,
                bucket=bucket,
                rubric_name="field_present",
                target=path,
                expected="<non-null>",
                observed=present_observed,
                passed=present_observed is not None,
            )
        )

    # field_abstains — reason at <path>.abstain_reason matches expected
    for path, expected_reason in (expectations.get("field_abstains") or {}).items():
        abstain_observed = resolve_path(facts, f"{path}.abstain_reason")
        out.append(
            RubricResult(
                case_id=case_id,
                bucket=bucket,
                rubric_name="field_abstains",
                target=f"{path}.abstain_reason",
                expected=expected_reason,
                observed=abstain_observed,
                passed=abstain_observed == expected_reason,
            )
        )

    return out


def _values_equal(expected: object, observed: object) -> bool:
    """Boolean equality with two specific affordances:

    * Strings compare case-insensitively (lab analyte names vary in
      case across labs; we don't want eval brittleness on that).
    * Numbers compare with a small absolute tolerance (a VLM might
      read 232 as 232.0, or vice versa).
    """

    if isinstance(expected, str) and isinstance(observed, str):
        return expected.strip().lower() == observed.strip().lower()
    if isinstance(expected, (int, float)) and isinstance(observed, (int, float)):
        return abs(float(expected) - float(observed)) < 0.01
    return expected == observed


def run_case(
    *,
    case: dict[str, Any],
    client: Anthropic,
    model: str,
    cases_dir: Path,
) -> CaseResult:
    """Extract one case's document and evaluate its rubrics."""

    case_id = str(case["case_id"])
    bucket = str(case.get("bucket", ""))

    raw_path = str(case["document_path"])
    document_path = (cases_dir / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path)
    if not document_path.exists():
        # Failed-load: report a single rubric failure so the runner
        # exits non-zero rather than silently skipping.
        return CaseResult(
            case_id=case_id,
            bucket=bucket,
            rubrics=[
                RubricResult(
                    case_id=case_id,
                    bucket=bucket,
                    rubric_name="document_loadable",
                    target="document_path",
                    expected="exists",
                    observed=str(document_path),
                    passed=False,
                    note="document not found",
                )
            ],
        )

    document_type: DocumentType = case.get("document_type", "lab_pdf")
    document_id = f"eval:{case_id}"

    try:
        result = extract(
            client=client,
            model=model,
            document_id=document_id,
            document_type=document_type,
            pdf_path=document_path,
        )
    except UnsupportedDocumentTypeError as exc:
        # Stubbed extractor — surface as a single failure rubric so
        # the runner exits non-zero without crashing other cases.
        return CaseResult(
            case_id=case_id,
            bucket=bucket,
            rubrics=[
                RubricResult(
                    case_id=case_id,
                    bucket=bucket,
                    rubric_name="extractor_implemented",
                    target=document_type,
                    expected="<implemented>",
                    observed="UNSUPPORTED_DOCUMENT_TYPE",
                    passed=False,
                    note=str(exc),
                )
            ],
        )
    except ExtractorError as exc:
        # Generic extractor failure (VLM error, schema mismatch, etc.).
        return CaseResult(
            case_id=case_id,
            bucket=bucket,
            rubrics=[
                RubricResult(
                    case_id=case_id,
                    bucket=bucket,
                    rubric_name="extractor_succeeds",
                    target=document_type,
                    expected="<no error>",
                    observed=type(exc).__name__,
                    passed=False,
                    note=str(exc),
                )
            ],
        )

    facts = result.facts.model_dump(mode="json")
    rubrics = evaluate_case(case, facts)
    return CaseResult(case_id=case_id, bucket=bucket, rubrics=rubrics)


def discover_cases(buckets: list[str]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for bucket in buckets:
        bucket_dir = CASES_ROOT / bucket
        if not bucket_dir.exists():
            continue
        for path in sorted(bucket_dir.glob("*.json")):
            with path.open() as fh:
                case = json.load(fh)
            case.setdefault("bucket", bucket)
            case.setdefault("case_id", f"{bucket}/{path.stem}")
            cases.append(case)
    return cases


def write_csv(results: list[CaseResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["case_id", "bucket", "rubric", "target", "expected", "observed", "passed", "note"]
        )
        for case in results:
            for rubric in case.rubrics:
                writer.writerow(
                    [
                        rubric.case_id,
                        rubric.bucket,
                        rubric.rubric_name,
                        rubric.target,
                        json.dumps(rubric.expected, default=str),
                        json.dumps(rubric.observed, default=str),
                        "PASS" if rubric.passed else "FAIL",
                        rubric.note,
                    ]
                )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="extraction_runner")
    p.add_argument(
        "--bucket",
        action="append",
        default=None,
        help="Bucket name to run (repeatable). Defaults to all buckets under tests/eval/cases/.",
    )
    p.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Write a per-rubric CSV of results to this path (for human verification).",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the slow-lane model (defaults to settings.model_slow).",
    )
    args = p.parse_args(argv)

    buckets = args.bucket or list(DEFAULT_BUCKETS)
    cases = discover_cases(buckets)
    if not cases:
        print(f"no cases found for buckets {buckets}; nothing to do.", file=sys.stderr)
        return 0

    settings = get_settings()
    if not settings.llm_api_key:
        print(
            "error: ANTHROPIC_API_KEY is not set; cannot run live extraction eval.",
            file=sys.stderr,
        )
        return 2

    client = Anthropic(api_key=settings.llm_api_key)
    model = args.model or settings.model_slow

    print(f"running {len(cases)} cases across buckets {buckets} (model={model})")
    results: list[CaseResult] = []
    for case in cases:
        case_id = str(case["case_id"])
        print(f"  · {case_id} … ", end="", flush=True)
        result = run_case(case=case, client=client, model=model, cases_dir=CASES_ROOT)
        results.append(result)
        passed = sum(1 for r in result.rubrics if r.passed)
        total = len(result.rubrics)
        flag = "PASS" if result.passed else "FAIL"
        print(f"{flag} ({passed}/{total} rubrics)")
        if not result.passed:
            for r in result.rubrics:
                if not r.passed:
                    print(
                        f"      - {r.rubric_name} target={r.target!r}: "
                        f"expected={r.expected!r} observed={r.observed!r}"
                    )

    overall_pass = all(r.passed for r in results)
    print()
    print(
        f"summary: {sum(1 for r in results if r.passed)}/{len(results)} cases pass; "
        f"overall {'PASS' if overall_pass else 'FAIL'}"
    )

    if args.csv_out is not None:
        write_csv(results, args.csv_out)
        print(f"wrote {args.csv_out}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
