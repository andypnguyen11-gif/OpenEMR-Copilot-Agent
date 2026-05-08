"""Critic node tests (W2-07).

Focuses on the deterministic check tier — every rejection branch fires
under its own trigger condition, the order of checks matches the
fastest-fails-first contract, and ACCEPT only fires when every branch
has passed. The LLM-judge tier is exercised only at the timeout-cap
boundary (forcing a synthetic >1.5s call must abort with
:data:`RejectionReason.JUDGE_TIMEOUT`).

The LLM judge body itself is well-trodden territory (single Anthropic
call with structured output via tool_use); the units we care about
proving are: (1) deterministic checks short-circuit before the judge
runs, (2) each rejection reason maps to its A.6 trigger, (3) the
1.5s cap is enforced.
"""

from __future__ import annotations

import time

import pytest

from clinical_copilot.orchestrator.critic import (
    deterministic_check,
    judge,
)
from clinical_copilot.orchestrator.state import (
    Citation,
    ClaimType,
    CriticVerdict,
    Draft,
    RejectionReason,
    SubQuery,
    Worker,
)

# --------------------------------------------------------------- helpers


def _sub_query(*, claim_type: ClaimType = ClaimType.GUIDELINE) -> SubQuery:
    return SubQuery(
        id="sq1",
        text="example",
        claim_type=claim_type,
        target_worker={
            ClaimType.CHART_FACT: Worker.CHART_TOOLS,
            ClaimType.DOC_FACT: Worker.INTAKE_EXTRACTOR,
            ClaimType.GUIDELINE: Worker.EVIDENCE_RETRIEVER,
        }[claim_type],
    )


def _draft(
    *,
    text: str = "prose",
    citations: tuple[Citation, ...] = (Citation(corpus_id="c1"),),
    abstain_reason: str | None = None,
) -> Draft:
    return Draft(
        sub_query_id="sq1",
        worker=Worker.EVIDENCE_RETRIEVER,
        text=text,
        citations=citations,
        abstain_reason=abstain_reason,
    )


# --------------------------------------------------------------- deterministic ACCEPT path


def test_deterministic_accept_when_all_checks_pass() -> None:
    """Guideline draft with a corpus_id citation, no action verbs,
    confidence at floor — should let the LLM judge handle it
    (deterministic returns None)."""

    sub_query = _sub_query(claim_type=ClaimType.GUIDELINE)
    draft = _draft(
        text="The guideline recommends annual screening for adults aged 35 to 70.",
        citations=(Citation(corpus_id="c1", confidence=0.85),),
    )

    assert deterministic_check(draft=draft, sub_query=sub_query) is None


def test_deterministic_accept_passes_through_worker_abstain() -> None:
    """A worker that abstained outright (NO_DATA) is not a critic
    failure; pass through unchanged."""

    sub_query = _sub_query()
    draft = _draft(text="", citations=(), abstain_reason="no_data")

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.ACCEPT


# --------------------------------------------------------------- deterministic REJECT branches


def test_no_citation_branch_fires_on_empty_citations() -> None:
    sub_query = _sub_query()
    draft = _draft(citations=())

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.NO_CITATION


def test_citation_type_mismatch_chart_claim_with_corpus_citation() -> None:
    """Planner said chart_fact; worker cited a corpus chunk. A.5
    chart-vs-corpus split is binding."""

    sub_query = _sub_query(claim_type=ClaimType.CHART_FACT)
    draft = _draft(citations=(Citation(corpus_id="c1"),))

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.CITATION_TYPE_MISMATCH


def test_citation_type_mismatch_guideline_claim_with_chart_citation() -> None:
    sub_query = _sub_query(claim_type=ClaimType.GUIDELINE)
    draft = _draft(citations=(Citation(source_id="Observation/123"),))

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.CITATION_TYPE_MISMATCH


@pytest.mark.parametrize(
    "phrase",
    [
        "Start metformin 500mg.",
        "Stop the lisinopril.",
        "Increase the dose to 100mg.",
        "Decrease the warfarin.",
        "Switch to insulin.",
        "Discontinue the statin.",
        "Recommend starting metformin.",
    ],
)
def test_action_blacklist_fires_on_each_listed_verb(phrase: str) -> None:
    sub_query = _sub_query()
    draft = _draft(
        text=phrase,
        citations=(Citation(corpus_id="c1"),),
    )

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.ACTION_BLACKLIST


def test_action_blacklist_does_not_fire_on_word_substring() -> None:
    """``\\b`` boundaries must keep ``starter`` / ``discontinuation``
    from triggering the action blacklist."""

    sub_query = _sub_query()
    draft = _draft(
        text="The starter dose is documented in the guideline; discontinuation criteria follow.",
        citations=(Citation(corpus_id="c1"),),
    )

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    # No ACTION_BLACKLIST trigger; deterministic returns None so the
    # LLM judge runs next (that path is exercised separately).
    assert verdict is None


def test_confidence_floor_rejects_below_0_7() -> None:
    sub_query = _sub_query()
    draft = _draft(
        citations=(Citation(corpus_id="c1", confidence=0.6),),
    )

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.CONFIDENCE_FLOOR


def test_confidence_floor_passes_when_unset() -> None:
    """Chart records often have ``None`` confidence — treated as
    'not applicable', should not trigger CONFIDENCE_FLOOR."""

    sub_query = _sub_query()
    draft = _draft(citations=(Citation(corpus_id="c1"),))  # confidence=None

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is None


# --------------------------------------------------------------- deterministic ordering


def test_no_citation_short_circuits_before_action_blacklist() -> None:
    """A draft with both 'start metformin' AND no citations should
    trip NO_CITATION first (fastest check) — proves order matters."""

    sub_query = _sub_query()
    draft = _draft(
        text="Start metformin.",
        citations=(),
    )

    verdict = deterministic_check(draft=draft, sub_query=sub_query)

    assert verdict is not None
    assert verdict.rejection_reason is RejectionReason.NO_CITATION


# --------------------------------------------------------------- LLM judge timeout cap


class _SlowAnthropic:
    """Stand-in Anthropic client whose ``messages.create`` blocks past
    the judge timeout. Used to assert :data:`RejectionReason.JUDGE_TIMEOUT`
    fires on a synthetic >1.5s call without burning a real model
    round-trip."""

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep = sleep_seconds

        class _Messages:
            def __init__(self, sleep: float) -> None:
                self._sleep = sleep

            def create(self, **_kwargs: object) -> None:
                time.sleep(self._sleep)
                msg = "should never reach this"
                raise AssertionError(msg)

        self.messages = _Messages(sleep_seconds)


def test_judge_timeout_cap_forces_abstain_with_judge_timeout() -> None:
    """Force the judge to take >0.2s with a 0.05s timeout. The verdict
    must be REJECT/JUDGE_TIMEOUT, not a hung wait or generic
    JUDGE_REJECTED."""

    sub_query = _sub_query()
    # Pass deterministic checks so the LLM judge runs.
    draft = _draft(
        text="The guideline lists annual screening for adults 35 to 70.",
        citations=(Citation(corpus_id="c1", confidence=0.9),),
    )
    client = _SlowAnthropic(sleep_seconds=0.2)

    verdict = judge(
        client=client,  # type: ignore[arg-type]
        model="m",
        draft=draft,
        sub_query=sub_query,
        timeout_seconds=0.05,
    )

    assert verdict.verdict is CriticVerdict.REJECT
    assert verdict.rejection_reason is RejectionReason.JUDGE_TIMEOUT
