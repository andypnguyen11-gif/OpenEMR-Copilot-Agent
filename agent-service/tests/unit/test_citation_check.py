"""Unit tests for citation existence (ARCHITECTURE §3 layer 3).

Drives :func:`find_unresolved_citations` directly so failures point at the
citation-check layer rather than the middleware that composes it. Layered
test_verification.py covers the same module through the middleware.
"""

from __future__ import annotations

from clinical_copilot.orchestrator.schemas import Card, CitedClaim
from clinical_copilot.tools.records import ProblemRecord, ToolResult
from clinical_copilot.verification.citation_check import (
    collect_source_ids,
    find_unresolved_citations,
)


def _problem(sid: str) -> ProblemRecord:
    return ProblemRecord(
        source_id=sid,
        code="44054006",
        display="T2DM",
        status="active",
    )


def _wrap(*records: ProblemRecord) -> list[ToolResult]:
    return [ToolResult(tool_name="get_problems", patient_id="101", records=list(records))]


def test_collect_source_ids_dedupes_across_results() -> None:
    a = _wrap(_problem("Condition/c1"))
    b = _wrap(_problem("Condition/c1"), _problem("Condition/c2"))
    assert collect_source_ids(a + b) == {"Condition/c1", "Condition/c2"}


def test_empty_inputs_resolve_to_empty_unresolved() -> None:
    assert find_unresolved_citations(claims=[], cards=[], tool_results=[]) == []


def test_resolved_claim_returns_empty() -> None:
    results = _wrap(_problem("Condition/c1"))
    claim = CitedClaim(text="T2DM.", source_id="Condition/c1")
    assert find_unresolved_citations(claims=[claim], cards=[], tool_results=results) == []


def test_fabricated_claim_source_id_is_returned() -> None:
    results = _wrap(_problem("Condition/c1"))
    claim = CitedClaim(text="HTN.", source_id="Condition/never-fetched")
    assert find_unresolved_citations(claims=[claim], cards=[], tool_results=results) == [
        "Condition/never-fetched"
    ]


def test_fabricated_card_source_id_is_returned() -> None:
    results = _wrap(_problem("Condition/c1"))
    card = Card(
        title="Active problems",
        kind="problems",
        source_ids=["Condition/c1", "Condition/fabricated"],
    )
    assert find_unresolved_citations(claims=[], cards=[card], tool_results=results) == [
        "Condition/fabricated"
    ]


def test_claim_side_unresolved_listed_before_card_side() -> None:
    """Stable ordering — the trust decision is invariant to order, but
    legible test failures are easier to debug when the list is stable."""

    results = _wrap(_problem("Condition/c1"))
    claim = CitedClaim(text="x", source_id="Condition/missing-claim")
    card = Card(
        title="x",
        kind="problems",
        source_ids=["Condition/missing-card"],
    )
    assert find_unresolved_citations(claims=[claim], cards=[card], tool_results=results) == [
        "Condition/missing-claim",
        "Condition/missing-card",
    ]


def test_duplicate_unresolved_id_listed_once() -> None:
    """If the model fabricates the same id three places, the abstention
    reason should mention it once. The de-dupe is observable through this."""

    results = _wrap(_problem("Condition/c1"))
    claim_a = CitedClaim(text="a", source_id="Condition/missing")
    claim_b = CitedClaim(text="b", source_id="Condition/missing")
    card = Card(title="x", kind="problems", source_ids=["Condition/missing"])
    assert find_unresolved_citations(
        claims=[claim_a, claim_b], cards=[card], tool_results=results
    ) == ["Condition/missing"]


def test_partial_resolution_only_returns_unresolved() -> None:
    """A draft that mixes valid and fabricated ids: only fabricated leak."""

    results = _wrap(_problem("Condition/c1"), _problem("Condition/c2"))
    claim_real = CitedClaim(text="real.", source_id="Condition/c1")
    claim_fake = CitedClaim(text="fake.", source_id="Condition/nope")
    card = Card(title="x", kind="problems", source_ids=["Condition/c2"])
    assert find_unresolved_citations(
        claims=[claim_real, claim_fake], cards=[card], tool_results=results
    ) == ["Condition/nope"]
