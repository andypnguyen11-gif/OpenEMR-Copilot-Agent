"""JSON + Markdown writers for the Stage 4A eval gate.

Result shape (committed under ``evals/extraction/results/<run_id>.json``):

.. code-block:: json

    {
      "run_id": "2026-05-06T18-00-00Z",
      "schema_version": 1,
      "case_count": 50,
      "summary": {
        "schema_valid":         {"passed": 50, "total": 50, "pass_rate": 1.00},
        "citation_present":     {"passed": 48, "total": 50, "pass_rate": 0.96},
        "factually_consistent": {"passed": 46, "total": 50, "pass_rate": 0.92},
        "safe_refusal":         {"passed": 8,  "total": 8,  "pass_rate": 1.00},
        "no_phi_in_logs":       {"passed": 50, "total": 50, "pass_rate": 1.00}
      },
      "cases": [
        {
          "case_id": "...",
          "bucket": "extraction",
          "outcomes": [
            {"rubric": "schema_valid", "passed": true,  "reason": ""},
            {"rubric": "citation_present", "passed": false, "reason": "..."}
          ]
        },
        ...
      ]
    }

The Markdown writer renders a per-rubric summary table plus a list of
failing cases — the artifact a grader can scan in 30 seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clinical_copilot.evals.extraction.cases import Case, RubricCategory
from clinical_copilot.evals.extraction.rubrics import RubricOutcome

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CategorySummary:
    rubric: RubricCategory
    passed: int
    total: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregated pass-rate per rubric across all cases that opted in."""

    run_id: str
    case_count: int
    categories: tuple[CategorySummary, ...]

    def category(self, rubric: RubricCategory) -> CategorySummary | None:
        for cat in self.categories:
            if cat.rubric is rubric:
                return cat
        return None


def make_run_id() -> str:
    """ISO 8601 UTC timestamp suitable for a filename (no colons)."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def summarize(results: list[tuple[Case, list[RubricOutcome]]]) -> RunSummary:
    """Aggregate per-case outcomes into a per-rubric summary."""

    counters: dict[RubricCategory, list[int]] = {cat: [0, 0] for cat in RubricCategory}
    for _, outcomes in results:
        for o in outcomes:
            counters[o.rubric][1] += 1
            if o.passed:
                counters[o.rubric][0] += 1

    categories = tuple(
        CategorySummary(rubric=rubric, passed=p, total=t)
        for rubric, (p, t) in counters.items()
        if t > 0
    )
    return RunSummary(run_id=make_run_id(), case_count=len(results), categories=categories)


def write_json(
    path: Path,
    summary: RunSummary,
    results: list[tuple[Case, list[RubricOutcome]]],
) -> None:
    payload = {
        "run_id": summary.run_id,
        "schema_version": SCHEMA_VERSION,
        "case_count": summary.case_count,
        "summary": {
            cat.rubric.value: {
                "passed": cat.passed,
                "total": cat.total,
                "pass_rate": round(cat.pass_rate, 4),
            }
            for cat in summary.categories
        },
        "cases": [
            {
                "case_id": case.case_id,
                "bucket": case.bucket.value,
                "outcomes": [
                    {
                        "rubric": o.rubric.value,
                        "passed": o.passed,
                        "reason": o.reason,
                    }
                    for o in outcomes
                ],
            }
            for case, outcomes in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def write_markdown(
    path: Path,
    summary: RunSummary,
    results: list[tuple[Case, list[RubricOutcome]]],
) -> None:
    lines: list[str] = []
    lines.append(f"# Extraction eval — {summary.run_id}")
    lines.append("")
    lines.append(f"Cases: {summary.case_count}")
    lines.append("")
    lines.append("| Rubric | Pass | Total | Rate |")
    lines.append("|---|---:|---:|---:|")
    for cat in summary.categories:
        lines.append(f"| {cat.rubric.value} | {cat.passed} | {cat.total} | {cat.pass_rate:.2%} |")
    lines.append("")
    failing = [
        (case, [o for o in outcomes if not o.passed])
        for case, outcomes in results
        if any(not o.passed for o in outcomes)
    ]
    if failing:
        lines.append("## Failures")
        lines.append("")
        for case, fails in failing:
            lines.append(f"### `{case.case_id}` ({case.bucket.value})")
            for o in fails:
                lines.append(f"- **{o.rubric.value}** — {o.reason}")
            lines.append("")
    else:
        lines.append("## Failures")
        lines.append("")
        lines.append("None — every rubric passed across all cases.")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
