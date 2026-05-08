"""Citation-separation eval cases (W2-07 acceptance gate).

Six fixture cases under ``evals/extraction/labels/citation_separation/``
exercise the chart-vs-corpus citation-type contract from PRD2 Appendix
A.5 and the rejection-reason taxonomy from A.6. Each case is a JSON
file shaped:

::

    {
      "case_id": "...",
      "description": "...",
      "claim_type": "chart_fact" | "doc_fact" | "guideline",
      "draft": {
        "text": "...",
        "citations": [{"source_id": "..." | "corpus_id": "..."}, ...]
      },
      "expected": {
        "verdict": "accept" | "reject",
        "rejection_reason": null | "no_citation" | "citation_type_mismatch" | ...
      }
    }

Loading the cases at test-collection time and parametrizing pytest
keeps each case a separate test ID in CI output so a single failure
points at exactly one fixture file.

The cases drive the deterministic checks in
:func:`clinical_copilot.orchestrator.critic.deterministic_check`. A
case whose ``expected.verdict`` is ``accept`` may either return a
deterministic ACCEPT (worker abstain pass-through, see cs06 if added)
or return ``None`` meaning the LLM judge would run; both paths
satisfy the W2-07 contract that the right citation type is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinical_copilot.orchestrator.critic import deterministic_check
from clinical_copilot.orchestrator.state import (
    Citation,
    ClaimType,
    CriticVerdict,
    Draft,
    RejectionReason,
    SubQuery,
    Worker,
)

CASES_DIR = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "extraction"
    / "labels"
    / "citation_separation"
)


def _claim_type_to_worker(claim_type: ClaimType) -> Worker:
    return {
        ClaimType.CHART_FACT: Worker.CHART_TOOLS,
        ClaimType.DOC_FACT: Worker.INTAKE_EXTRACTOR,
        ClaimType.GUIDELINE: Worker.EVIDENCE_RETRIEVER,
    }[claim_type]


def _build_draft(payload: dict[str, object]) -> Draft:
    raw_citations = payload.get("citations", [])
    assert isinstance(raw_citations, list)
    citations = tuple(
        Citation(
            source_id=str(c["source_id"]) if c.get("source_id") else None,
            corpus_id=str(c["corpus_id"]) if c.get("corpus_id") else None,
            confidence=float(c["confidence"]) if c.get("confidence") is not None else None,
        )
        for c in raw_citations
        if isinstance(c, dict)
    )
    return Draft(
        sub_query_id="sq-fixture",
        worker=Worker.EVIDENCE_RETRIEVER,
        text=str(payload.get("text", "")),
        citations=citations,
    )


def _load_cases() -> list[dict[str, object]]:
    return sorted(
        (json.loads(path.read_text(encoding="utf-8")) for path in CASES_DIR.glob("*.json")),
        key=lambda c: c["case_id"],
    )


CASES = _load_cases()


def test_six_citation_separation_cases_authored() -> None:
    """The W2-07 acceptance gate names exactly 6 cases."""

    assert len(CASES) == 6, f"expected 6 citation_separation cases, found {len(CASES)}"


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[str(c["case_id"]) for c in CASES],
)
def test_citation_separation_case(case: dict[str, object]) -> None:
    """Each case asserts the deterministic critic produces the
    expected verdict for the (claim_type, citation kind) pair.

    Accept paths may return ``None`` from the deterministic check
    (meaning the LLM judge would run next on the live path) — the
    W2-07 contract is satisfied either way: the deterministic layer
    didn't reject, so the citation kind is consistent with the
    claim_type.
    """

    claim_type = ClaimType(str(case["claim_type"]))
    sub_query = SubQuery(
        id="sq-fixture",
        text="fixture",
        claim_type=claim_type,
        target_worker=_claim_type_to_worker(claim_type),
    )
    draft_payload = case["draft"]
    assert isinstance(draft_payload, dict)
    draft = _build_draft(draft_payload)
    expected = case["expected"]
    assert isinstance(expected, dict)
    expected_verdict = expected["verdict"]

    actual = deterministic_check(draft=draft, sub_query=sub_query)

    if expected_verdict == "accept":
        # Either deterministic accepted (e.g. worker-abstain pass-through)
        # or it returned None to defer to the LLM judge — both prove the
        # citation kind is consistent with the claim type.
        if actual is not None:
            assert actual.verdict is CriticVerdict.ACCEPT, (
                f"{case['case_id']}: expected accept-or-None, "
                f"got {actual.verdict.value} {actual.rejection_reason}"
            )
        return

    assert actual is not None, f"{case['case_id']}: expected reject, got None (LLM-judge defer)"
    assert actual.verdict is CriticVerdict.REJECT, (
        f"{case['case_id']}: expected reject, got {actual.verdict.value}"
    )
    expected_reason = expected["rejection_reason"]
    if expected_reason is not None:
        assert actual.rejection_reason is RejectionReason(expected_reason), (
            f"{case['case_id']}: expected {expected_reason}, "
            f"got {actual.rejection_reason}"
        )
