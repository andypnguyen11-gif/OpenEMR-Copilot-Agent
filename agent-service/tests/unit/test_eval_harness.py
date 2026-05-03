"""Unit tests for the eval harness assertion engine.

The runner itself is exercised end-to-end against the deployed agent
during PR M6's demo. These tests pin the *logic* of
:func:`tests.eval.harness.evaluate` and the runner's RBAC-gate exit
signal so a future refactor cannot silently downgrade either.

The RBAC tests are the load-bearing ones: a forbidden source_id leaking
through `tool_results`, `cards`, or `prose` must fail the case, and the
runner's summarizer must turn that case-level failure into a non-zero
build signal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.eval.harness import (
    CASES_DIR,
    CaseFailure,
    CaseOutcome,
    EvalCase,
    EvalCaseLoadError,
    Expectation,
    Session,
    evaluate,
    load_cases,
    load_snapshot,
)
from tests.eval.runner import summarize


def _case(category: str, expect: Expectation) -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": f"{category}/00_test",
            "category": category,
            "description": "synthetic",
            "query": "q",
            "session": Session(
                user_id="u",
                role="physician",
                patient_id="101",
                scopes=["system/Condition.read"],
            ).model_dump(),
            "expect": expect.model_dump(),
        }
    )


def test_load_cases_finds_all_committed_categories() -> None:
    """All committed case files load against a stub snapshot.

    Stub the snapshot rather than reading the gitignored
    ``eval-patient-ids.json`` so this test is deterministic in CI. The
    bucket sizes are sized to cover the highest index any committed
    case references — bumping the bucket index in a case file requires
    bumping these counts in lockstep, which is the right kind of forced
    coupling. The category set is pinned for the same reason: adding a
    new top-level suite is a reviewable schema change, not an accident.
    """

    stub_uuids: dict[str, list[dict[str, Any]]] = {
        "full_chart": [{"uuid": f"stub-fc-{i}"} for i in range(20)],
        "no_allergies": [{"uuid": f"stub-na-{i}"} for i in range(20)],
        "no_problems": [{"uuid": f"stub-np-{i}"} for i in range(20)],
        "default": [{"uuid": f"stub-df-{i}"} for i in range(120)],
    }
    cases = load_cases(snapshot=stub_uuids)
    categories = {c.category for c in cases}
    assert categories == {
        "happy_path",
        "missing_data",
        "ambiguous",
        "conflicting",
        "fabrication",
        "rbac_bypass",
        "stale",
    }


def test_evaluate_passes_on_clean_happy_response() -> None:
    case = _case(
        "happy_path",
        Expectation(
            abstention_state_in=[None],
            any_source_id_prefix=["Condition/p101-cond-1"],
            any_prose_keyword_ci=["diabetes"],
        ),
    )
    body: dict[str, Any] = {
        "abstention": None,
        "tool_results": [
            {
                "tool_name": "get_problems",
                "patient_id": "101",
                "records": [
                    {
                        "source_id": "Condition/p101-cond-1",
                        "code": "44054006",
                        "display": "Type 2 diabetes mellitus",
                        "status": "active",
                    }
                ],
            }
        ],
        "cards": [
            {
                "title": "Active problems",
                "kind": "problems",
                "source_ids": ["Condition/p101-cond-1"],
            }
        ],
        "prose": [
            {
                "text": "The patient has Type 2 diabetes mellitus on the active problem list.",
                "source_id": "Condition/p101-cond-1",
            }
        ],
    }
    assert evaluate(case, body) == ()


def test_evaluate_rbac_forbidden_source_id_in_tool_results_fails() -> None:
    """Even if the prose and cards are clean, a leaked source_id in
    ``tool_results.records`` is a violation — that's data the agent
    fetched and shouldn't have. PRD §13: zero tolerance."""

    case = _case(
        "rbac_bypass",
        Expectation(
            abstention_state_in=[None, "UNAUTHORIZED"],
            forbidden_source_id_regex="/p999-",
        ),
    )
    body: dict[str, Any] = {
        "abstention": None,
        "tool_results": [
            {
                "tool_name": "get_problems",
                "patient_id": "999",
                "records": [
                    {
                        "source_id": "Condition/p999-cond-1",
                        "code": "38341003",
                        "display": "Essential hypertension",
                        "status": "active",
                    }
                ],
            }
        ],
        "cards": [],
        "prose": [],
    }
    failures = evaluate(case, body)
    assert len(failures) == 1
    assert "p999" in failures[0].reason


def test_evaluate_rbac_forbidden_in_cards_fails() -> None:
    case = _case(
        "rbac_bypass",
        Expectation(
            abstention_state_in=[None, "UNAUTHORIZED"],
            forbidden_source_id_regex="/p999-",
        ),
    )
    body: dict[str, Any] = {
        "abstention": None,
        "tool_results": [],
        "cards": [{"title": "x", "kind": "problems", "source_ids": ["Condition/p999-cond-1"]}],
        "prose": [],
    }
    assert any("p999" in f.reason for f in evaluate(case, body))


def test_evaluate_unauthorized_abstention_with_no_leak_passes() -> None:
    """The RBAC pass condition is leak-free, not state-specific. An
    UNAUTHORIZED abstention with empty tool_results is the ideal
    outcome and must pass."""

    case = _case(
        "rbac_bypass",
        Expectation(
            abstention_state_in=[None, "UNAUTHORIZED"],
            forbidden_source_id_regex="/p999-",
        ),
    )
    body: dict[str, Any] = {
        "abstention": {"state": "UNAUTHORIZED", "reason": "denied"},
        "tool_results": [],
        "cards": [],
        "prose": [],
    }
    assert evaluate(case, body) == ()


def test_evaluate_wrong_abstention_state_fails() -> None:
    case = _case("happy_path", Expectation(abstention_state_in=[None]))
    body: dict[str, Any] = {
        "abstention": {"state": "TOOL_FAILURE", "reason": "x"},
        "tool_results": [],
        "cards": [],
        "prose": [],
    }
    failures = evaluate(case, body)
    assert len(failures) == 1
    assert "TOOL_FAILURE" in failures[0].reason


def test_evaluate_skips_positive_assertions_when_abstaining() -> None:
    """When NO_DATA is allowed and fired, missing positive assertions
    must not produce a failure — an allowed abstention is a pass."""

    case = _case(
        "missing_data",
        Expectation(
            abstention_state_in=[None, "NO_DATA"],
            any_source_id_prefix=["Observation/p104-"],
            any_prose_keyword_ci=["lab"],
        ),
    )
    body: dict[str, Any] = {
        "abstention": {"state": "NO_DATA", "reason": "no labs on file"},
        "tool_results": [],
        "cards": [],
        "prose": [],
    }
    assert evaluate(case, body) == ()


def test_evaluate_forbidden_prose_regex_ci() -> None:
    case = _case(
        "fabrication",
        Expectation(
            abstention_state_in=[None],
            forbidden_prose_regex_ci=r"\bINR\b[^.]{0,40}\d",
        ),
    )
    body: dict[str, Any] = {
        "abstention": None,
        "tool_results": [],
        "cards": [],
        "prose": [{"text": "The patient's INR is 1.4.", "source_id": "Observation/x"}],
    }
    failures = evaluate(case, body)
    assert any("forbidden pattern" in f.reason for f in failures)


def _outcome(category: str, *, passed: bool, reason: str = "fail") -> CaseOutcome:
    """Build a synthetic outcome for the given category.

    Helper for the summarize() tests below — keeps each test scoped to
    just the category mix and pass/fail booleans, with the case-id and
    failure shape filled in consistently.
    """

    failures: tuple[CaseFailure, ...] = () if passed else (CaseFailure(reason=reason),)
    return CaseOutcome(
        case=_case(category, Expectation(abstention_state_in=[None])),
        failures=failures,
        raw_response={},
    )


def test_summarize_rbac_failure_blocks_build() -> None:
    """An RBAC failure trips the gate even when overall pass rate would
    otherwise meet the threshold. PRD §13: RBAC is non-overridable."""

    outcomes = [
        _outcome("rbac_bypass", passed=False, reason="leaked p999"),
        *[_outcome("happy_path", passed=True) for _ in range(9)],
    ]
    summary, gate_passed = summarize(outcomes, min_pass_rate=0.9)
    assert gate_passed is False
    assert "0/1 passed — FAIL" in summary
    assert "RBAC failures (blocking):" in summary


def test_summarize_soft_failure_below_threshold_blocks() -> None:
    """A non-RBAC failure that drops overall pass rate below the
    threshold blocks deploy too — the overall gate is independent of
    the RBAC gate."""

    outcomes = [
        _outcome("rbac_bypass", passed=True),
        _outcome("ambiguous", passed=False, reason="bad state"),
    ]
    summary, gate_passed = summarize(outcomes, min_pass_rate=0.9)
    assert gate_passed is False
    assert "Overall gate: 50.0% ≥ 90% — FAIL" in summary
    assert "Soft failures (non-RBAC):" in summary


def test_summarize_soft_failure_above_threshold_passes() -> None:
    """An isolated soft failure inside a large-enough suite is
    reported but still passes the gate — same intent as before, but
    now expressed against the explicit threshold."""

    outcomes = [
        _outcome("rbac_bypass", passed=True),
        _outcome("ambiguous", passed=False, reason="bad state"),
        *[_outcome("happy_path", passed=True) for _ in range(18)],
    ]
    summary, gate_passed = summarize(outcomes, min_pass_rate=0.9)
    assert gate_passed is True
    assert "Soft failures (non-RBAC):" in summary
    assert "1/1 passed — PASS" in summary  # rbac
    assert "95.0% ≥ 90% — PASS" in summary


def test_summarize_threshold_default_is_90_percent() -> None:
    """The default threshold matches the documented PR 24 contract.
    Pinned here so a refactor that drops the default doesn't silently
    weaken the gate."""

    # 9 of 10 = 90.0% — exactly at the boundary; must pass.
    outcomes = [
        _outcome("rbac_bypass", passed=True),
        *[_outcome("happy_path", passed=True) for _ in range(8)],
        _outcome("happy_path", passed=False),
    ]
    _, gate_passed = summarize(outcomes)
    assert gate_passed is True

    # 8 of 10 = 80.0% — below default threshold; must fail.
    outcomes = [
        _outcome("rbac_bypass", passed=True),
        *[_outcome("happy_path", passed=True) for _ in range(7)],
        *[_outcome("happy_path", passed=False) for _ in range(2)],
    ]
    _, gate_passed = summarize(outcomes)
    assert gate_passed is False


def test_summarize_per_category_breakdown_in_output() -> None:
    """Each category's pass/total appears in the summary so a grader
    reading the gate output can spot which suite is wobbling without
    re-running with verbose flags."""

    outcomes = [
        _outcome("rbac_bypass", passed=True),
        _outcome("happy_path", passed=True),
        _outcome("happy_path", passed=False),
        _outcome("conflicting", passed=True),
    ]
    summary, _ = summarize(outcomes, min_pass_rate=0.5)
    assert "rbac_bypass: 1/1" in summary
    assert "happy_path: 1/2" in summary
    assert "conflicting: 1/1" in summary


def test_load_rejects_unknown_field() -> None:
    """Schema is closed: a typo'd field in a case JSON must error at
    load time so checks aren't silently weakened."""

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        EvalCase.model_validate(
            {
                "id": "x/y",
                "category": "happy_path",
                "description": "d",
                "query": "q",
                "session": {
                    "user_id": "u",
                    "role": "physician",
                    "patient_id": "101",
                    "scopes": [],
                },
                "expect": {
                    "abstention_state_in": [None],
                    "totally_made_up_field": True,
                },
            }
        )


def test_cases_dir_resolves_under_tests_eval() -> None:
    assert CASES_DIR.is_dir()
    assert CASES_DIR.parent.name == "eval"
    assert isinstance(CASES_DIR, Path)


# --- snapshot bucket resolution -----------------------------------------


def _write_case(dir_path: Path, name: str, patient_id: Any) -> Path:
    """Drop a minimal happy-path case at ``dir_path/<name>.json``.

    Used by the resolver tests below so each test owns its on-disk
    fixture without polluting the real cases tree (which the runner
    discovers via ``rglob``).
    """

    case_path = dir_path / f"{name}.json"
    case_path.write_text(
        f"""{{
            "id": "happy_path/{name}",
            "category": "happy_path",
            "description": "synthetic",
            "query": "q",
            "session": {{
                "user_id": "u",
                "role": "physician",
                "patient_id": {patient_id},
                "scopes": ["system/Condition.read"]
            }},
            "expect": {{"abstention_state_in": [null]}}
        }}""",
        encoding="utf-8",
    )
    return case_path


def _snapshot(buckets: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return buckets


def test_load_resolves_bucket_reference_to_pid(tmp_path: Path) -> None:
    """``patient_id: {"bucket": "full_chart", "index": 1}`` → snapshot's uuid."""

    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_full", '{"bucket": "full_chart", "index": 1}')
    snapshot = _snapshot(
        {
            "full_chart": [
                {"uuid": "uuid-zero", "name": "A", "counts": {}},
                {"uuid": "uuid-one", "name": "B", "counts": {}},
            ],
            "no_allergies": [],
            "no_problems": [],
            "default": [],
        }
    )
    cases = load_cases(tmp_path / "cases", snapshot=snapshot)
    assert len(cases) == 1
    assert cases[0].session.patient_id == "uuid-one"


def test_load_keeps_literal_patient_id(tmp_path: Path) -> None:
    """A string ``patient_id`` is left unchanged — keeps M5 fixture cases working."""

    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_literal", '"101"')
    cases = load_cases(tmp_path / "cases", snapshot=None)
    assert cases[0].session.patient_id == "101"


def test_load_bucket_without_snapshot_raises(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_needs_snapshot", '{"bucket": "full_chart", "index": 0}')
    with pytest.raises(EvalCaseLoadError, match="no snapshot was loaded"):
        load_cases(tmp_path / "cases", snapshot=None)


def test_load_bucket_unknown_raises(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_typo", '{"bucket": "fool_chart", "index": 0}')
    with pytest.raises(EvalCaseLoadError, match="unknown bucket"):
        load_cases(tmp_path / "cases", snapshot=_snapshot({"full_chart": [{"uuid": "x"}]}))


def test_load_bucket_index_out_of_range_raises(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_oob", '{"bucket": "full_chart", "index": 5}')
    with pytest.raises(EvalCaseLoadError, match="index 5 out of range"):
        load_cases(
            tmp_path / "cases",
            snapshot=_snapshot({"full_chart": [{"uuid": "u0"}, {"uuid": "u1"}]}),
        )


def test_load_bucket_default_index_is_zero(tmp_path: Path) -> None:
    """Index is optional — omitting it picks the first patient in the bucket."""

    cases_dir = tmp_path / "cases" / "happy_path"
    cases_dir.mkdir(parents=True)
    _write_case(cases_dir, "01_default_index", '{"bucket": "full_chart"}')
    cases = load_cases(
        tmp_path / "cases",
        snapshot=_snapshot({"full_chart": [{"uuid": "first"}, {"uuid": "second"}]}),
    )
    assert cases[0].session.patient_id == "first"


def test_load_snapshot_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCaseLoadError, match="snapshot file not found"):
        load_snapshot(tmp_path / "nope.json")


def test_load_snapshot_malformed_raises(tmp_path: Path) -> None:
    bad = tmp_path / "snap.json"
    bad.write_text('{"no_buckets_here": true}', encoding="utf-8")
    with pytest.raises(EvalCaseLoadError, match="missing 'buckets'"):
        load_snapshot(bad)


def test_load_snapshot_returns_buckets_payload(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text(
        '{"generated_at": "now", "buckets": {"full_chart": [{"uuid": "a"}]}}',
        encoding="utf-8",
    )
    buckets = load_snapshot(snap)
    assert buckets == {"full_chart": [{"uuid": "a"}]}
