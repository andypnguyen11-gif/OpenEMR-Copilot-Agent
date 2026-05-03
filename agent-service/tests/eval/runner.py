"""CLI entrypoint: drives every eval case against a deployed agent.

Invoke via ``python -m tests.eval.runner`` (or ``make eval``). The
runner POSTs each case to ``$AGENT_BASE_URL/api/agent/query`` with a
freshly minted JWT, runs the case's assertions over the response, and
prints a pass/fail line per case followed by a summary.

Two gates determine the exit code (PR 24):

* **RBAC gate (hard):** any failure in the ``rbac_bypass`` category
  fails the run. PRD §13 — security is stop-ship and not overridable.
* **Overall pass-rate gate:** overall pass rate must be ≥
  ``--min-pass-rate`` (default 0.9). Below that, the run fails even if
  RBAC is clean — a sub-90% suite means the agent is too unreliable to
  ship regardless of which categories are wobbling.

Either gate failing exits non-zero, blocking ``make deploy`` (which
depends on ``make eval``).

Configuration:

* ``--base-url`` / ``AGENT_BASE_URL`` (default ``http://localhost:8000``)
* ``--secret`` / ``COPILOT_HMAC_SECRET`` (must match the deployed
  service's secret; required)
* ``--cases`` (default ``tests/eval/cases/``)
* ``--timeout`` HTTP timeout per request, seconds (default 60)
* ``--min-pass-rate`` / ``EVAL_MIN_PASS_RATE`` (default 0.9; range 0-1)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests.eval.harness import (
    CASES_DIR,
    CaseFailure,
    CaseOutcome,
    EvalCase,
    evaluate,
    load_cases,
    load_snapshot,
    mint_jwt,
)
from tests.eval.persistence import EvalRunWriter, writer_from_database_url

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MIN_PASS_RATE = 0.9
# Snapshot file lives at the agent-service root by convention (next to the
# scripts/ that produces it). The default lookup is relative to the cases
# dir's grandparent — ``tests/eval/cases/`` → ``agent-service/`` — so a
# vanilla ``make eval`` run finds it without an extra flag.
DEFAULT_SNAPSHOT_PATH = CASES_DIR.parent.parent.parent / "eval-patient-ids.json"


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    base_url: str
    secret: str
    cases_dir: Path
    timeout_seconds: float
    snapshot_path: Path | None
    database_url: str | None
    min_pass_rate: float


def parse_args(argv: list[str]) -> RunnerConfig:
    parser = argparse.ArgumentParser(
        prog="tests.eval.runner",
        description="Run the 6-case eval suite against a deployed agent.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_BASE_URL", DEFAULT_BASE_URL),
        help="Base URL of the deployed agent service.",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("COPILOT_HMAC_SECRET"),
        help="HMAC secret for signing eval JWTs (must match the deployed service).",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=CASES_DIR,
        help="Directory containing case JSON files.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-request HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help=(
            "Path to eval-patient-ids.json (output of snapshot_eval_patients.py). "
            "Required when any case references a snapshot bucket; ignored otherwise. "
            f"Defaults to {DEFAULT_SNAPSHOT_PATH} if it exists."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help=(
            "SQLAlchemy URL for the agent-db (eval_runs persistence). "
            "Defaults to $DATABASE_URL; unset disables persistence."
        ),
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=float(os.environ.get("EVAL_MIN_PASS_RATE", DEFAULT_MIN_PASS_RATE)),
        help=(
            "Minimum overall pass rate (0-1) required to exit zero. "
            "Default 0.9; sub-threshold runs fail even if RBAC is clean."
        ),
    )
    args = parser.parse_args(argv)
    if not args.secret:
        parser.error("--secret or COPILOT_HMAC_SECRET is required")
    if not 0.0 <= args.min_pass_rate <= 1.0:
        parser.error(f"--min-pass-rate must be in [0, 1], got {args.min_pass_rate}")
    snapshot_path = args.snapshot
    if snapshot_path is None and DEFAULT_SNAPSHOT_PATH.is_file():
        snapshot_path = DEFAULT_SNAPSHOT_PATH
    return RunnerConfig(
        base_url=str(args.base_url).rstrip("/"),
        secret=str(args.secret),
        cases_dir=Path(args.cases),
        timeout_seconds=float(args.timeout),
        snapshot_path=snapshot_path,
        database_url=args.database_url or None,
        min_pass_rate=float(args.min_pass_rate),
    )


def run_case(case: EvalCase, *, client: httpx.Client, config: RunnerConfig) -> CaseOutcome:
    """Call the agent for one case and run its assertions.

    Transport-level failures (network, non-2xx, malformed JSON) collapse
    into a single ``transport_error`` so the summary can distinguish
    "agent answered but got it wrong" from "agent never answered".
    """

    token = mint_jwt(session=case.session, secret=config.secret)
    try:
        response = client.post(
            f"{config.base_url}/api/agent/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": case.query},
            timeout=config.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return CaseOutcome(
            case=case,
            failures=(),
            raw_response=None,
            transport_error=f"transport: {exc!s}",
        )

    if response.status_code != httpx.codes.OK:
        return CaseOutcome(
            case=case,
            failures=(),
            raw_response=None,
            transport_error=f"HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        return CaseOutcome(
            case=case,
            failures=(),
            raw_response=None,
            transport_error=f"non-JSON response: {exc!s}",
        )

    failures = evaluate(case, body)
    return CaseOutcome(case=case, failures=failures, raw_response=body)


def format_outcome(outcome: CaseOutcome) -> str:
    status = "PASS" if outcome.passed else "FAIL"
    lines = [f"{status}  {outcome.case.case_id}"]
    if outcome.transport_error:
        lines.append(f"        transport: {outcome.transport_error}")
    for failure in outcome.failures:
        lines.append(f"        - {failure.reason}")
    return "\n".join(lines)


def summarize(
    outcomes: list[CaseOutcome],
    *,
    min_pass_rate: float = DEFAULT_MIN_PASS_RATE,
) -> tuple[str, bool]:
    """Return ``(printable summary, gate_passed)``.

    ``gate_passed`` is the runner's exit-code signal — True only when
    *both* gates pass:

    * **RBAC gate:** every case in the ``rbac_bypass`` category passed.
    * **Overall gate:** overall pass rate ≥ ``min_pass_rate``.

    The summary prints both gate verdicts and a per-category breakdown
    so the failure cause is auditable from the gate output (PR 24
    spec). Soft (non-RBAC) failures stay listed separately so the
    historical "RBAC is the only hard gate" framing is still visible
    when the overall gate is what trips.
    """

    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.passed)
    failed = total - passed
    pass_rate = passed / total if total else 0.0

    rbac_outcomes = [o for o in outcomes if o.case.is_rbac_gate]
    rbac_total = len(rbac_outcomes)
    rbac_passed_count = sum(1 for o in rbac_outcomes if o.passed)
    rbac_passed = rbac_total > 0 and rbac_passed_count == rbac_total

    overall_passed = pass_rate >= min_pass_rate
    gate_passed = rbac_passed and overall_passed

    soft_failures = [o.case.case_id for o in outcomes if not o.passed and not o.case.is_rbac_gate]
    rbac_failures = [o.case.case_id for o in outcomes if not o.passed and o.case.is_rbac_gate]

    per_category: dict[str, tuple[int, int]] = {}
    for outcome in outcomes:
        cat = outcome.case.category
        cat_passed, cat_total = per_category.get(cat, (0, 0))
        per_category[cat] = (cat_passed + (1 if outcome.passed else 0), cat_total + 1)

    lines = [
        "=" * 64,
        f"Eval results: {passed} passed, {failed} failed ({total} total)",
        f"  pass rate: {pass_rate:.1%} (threshold {min_pass_rate:.0%})",
    ]
    for cat in sorted(per_category):
        cat_passed, cat_total = per_category[cat]
        lines.append(f"    {cat}: {cat_passed}/{cat_total}")
    lines.extend(
        [
            f"RBAC gate:    {rbac_passed_count}/{rbac_total} passed — "
            f"{'PASS' if rbac_passed else 'FAIL'}",
            f"Overall gate: {pass_rate:.1%} ≥ {min_pass_rate:.0%} — "
            f"{'PASS' if overall_passed else 'FAIL'}",
            "=" * 64,
        ]
    )
    if soft_failures:
        lines.append(f"Soft failures (non-RBAC): {', '.join(soft_failures)}")
    if rbac_failures:
        lines.append(f"RBAC failures (blocking): {', '.join(rbac_failures)}")
    return "\n".join(lines), gate_passed


def main(argv: list[str] | None = None) -> int:
    config = parse_args(sys.argv[1:] if argv is None else argv)
    snapshot = load_snapshot(config.snapshot_path) if config.snapshot_path else None
    cases = load_cases(config.cases_dir, snapshot=snapshot)
    if not cases:
        print(f"no cases found under {config.cases_dir}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    writer: EvalRunWriter = writer_from_database_url(config.database_url)

    print(f"Running {len(cases)} cases against {config.base_url} (run_id={run_id}) ...\n")
    outcomes: list[CaseOutcome] = []
    with httpx.Client() as client:
        for case in cases:
            outcome = run_case(case, client=client, config=config)
            outcomes.append(outcome)
            print(format_outcome(outcome))

    persisted = writer.write(outcomes, run_id=run_id)
    summary, gate_passed = summarize(outcomes, min_pass_rate=config.min_pass_rate)
    print()
    print(summary)
    if persisted:
        print(f"Persisted {persisted} eval_runs row(s) under run_id={run_id}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


# Re-export for tests.
__all__ = [
    "CaseFailure",
    "CaseOutcome",
    "RunnerConfig",
    "format_outcome",
    "main",
    "parse_args",
    "run_case",
    "summarize",
]
