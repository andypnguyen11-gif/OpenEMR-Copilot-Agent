"""CLI entrypoint: drives every eval case against a deployed agent.

Invoke via ``python -m tests.eval.runner`` (or ``make eval``). The
runner POSTs each case to ``$AGENT_BASE_URL/api/agent/query`` with a
freshly minted JWT, runs the case's assertions over the response, and
prints a pass/fail line per case followed by a summary.

Hard gate (PRD §13): any failure in the ``rbac_bypass`` category causes
a non-zero exit. Failures in other categories are reported but do not
fail the build — Thursday's agent is a fixture-driven MVP and we expect
the LLM to produce some category-level wobble that's still useful to
surface in the summary without blocking deploy.

Configuration:

* ``--base-url`` / ``AGENT_BASE_URL`` (default ``http://localhost:8000``)
* ``--secret`` / ``COPILOT_HMAC_SECRET`` (must match the deployed
  service's secret; required)
* ``--cases`` (default ``tests/eval/cases/``)
* ``--timeout`` HTTP timeout per request, seconds (default 60)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
    mint_jwt,
)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    base_url: str
    secret: str
    cases_dir: Path
    timeout_seconds: float


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
    args = parser.parse_args(argv)
    if not args.secret:
        parser.error("--secret or COPILOT_HMAC_SECRET is required")
    return RunnerConfig(
        base_url=str(args.base_url).rstrip("/"),
        secret=str(args.secret),
        cases_dir=Path(args.cases),
        timeout_seconds=float(args.timeout),
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


def summarize(outcomes: list[CaseOutcome]) -> tuple[str, bool]:
    """Return (printable summary, rbac_passed).

    ``rbac_passed`` is the runner's exit-code signal: True only when
    every case in the rbac_bypass category passed.
    """

    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.passed)
    failed = total - passed

    rbac_outcomes = [o for o in outcomes if o.case.is_rbac_gate]
    rbac_total = len(rbac_outcomes)
    rbac_passed_count = sum(1 for o in rbac_outcomes if o.passed)
    rbac_passed = rbac_total > 0 and rbac_passed_count == rbac_total

    soft_failures = [o.case.case_id for o in outcomes if not o.passed and not o.case.is_rbac_gate]
    rbac_failures = [o.case.case_id for o in outcomes if not o.passed and o.case.is_rbac_gate]

    lines = [
        "=" * 64,
        f"Eval results: {passed} passed, {failed} failed ({total} total)",
        f"RBAC gate: {rbac_passed_count}/{rbac_total} passed — {'PASS' if rbac_passed else 'FAIL'}",
        "=" * 64,
    ]
    if soft_failures:
        lines.append(f"Soft failures (non-blocking): {', '.join(soft_failures)}")
    if rbac_failures:
        lines.append(f"RBAC failures (blocking):     {', '.join(rbac_failures)}")
    return "\n".join(lines), rbac_passed


def main(argv: list[str] | None = None) -> int:
    config = parse_args(sys.argv[1:] if argv is None else argv)
    cases = load_cases(config.cases_dir)
    if not cases:
        print(f"no cases found under {config.cases_dir}", file=sys.stderr)
        return 2

    print(f"Running {len(cases)} cases against {config.base_url} ...\n")
    outcomes: list[CaseOutcome] = []
    with httpx.Client() as client:
        for case in cases:
            outcome = run_case(case, client=client, config=config)
            outcomes.append(outcome)
            print(format_outcome(outcome))

    summary, rbac_passed = summarize(outcomes)
    print()
    print(summary)
    return 0 if rbac_passed else 1


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
