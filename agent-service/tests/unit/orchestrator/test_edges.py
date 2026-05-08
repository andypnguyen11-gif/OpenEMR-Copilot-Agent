"""Pure-function tests for the LangGraph routing predicates (W2-07).

The route functions are deterministic given a :class:`TurnState`, so
every test here is a fresh state literal mapped to the expected route
label. No Anthropic, no graph compilation — fast and offline.
"""

from __future__ import annotations

import pytest

from clinical_copilot.orchestrator.edges import (
    ROUTE_ABSTAIN,
    ROUTE_FAN_OUT,
    ROUTE_RETRY,
    ROUTE_V1_SINGLE,
    ROUTE_VERIFICATION,
    route_after_critic,
    route_after_planner,
)
from clinical_copilot.orchestrator.state import (
    Citation,
    ClaimType,
    CriticVerdict,
    Draft,
    RejectionReason,
    SubQuery,
    Verdict,
    Worker,
    initial_state,
)

# --------------------------------------------------------------- helpers


def _sub_query(*, claim_type: ClaimType, sq_id: str = "sq1") -> SubQuery:
    return SubQuery(
        id=sq_id,
        text="example",
        claim_type=claim_type,
        target_worker={
            ClaimType.CHART_FACT: Worker.CHART_TOOLS,
            ClaimType.DOC_FACT: Worker.INTAKE_EXTRACTOR,
            ClaimType.GUIDELINE: Worker.EVIDENCE_RETRIEVER,
        }[claim_type],
    )


def _draft(*, sq_id: str, with_citation: bool = True) -> Draft:
    return Draft(
        sub_query_id=sq_id,
        worker=Worker.EVIDENCE_RETRIEVER,
        text="prose",
        citations=(Citation(corpus_id="c1"),) if with_citation else (),
    )


def _verdict(*, sq_id: str, accept: bool) -> Verdict:
    return Verdict(
        sub_query_id=sq_id,
        verdict=CriticVerdict.ACCEPT if accept else CriticVerdict.REJECT,
        rejection_reason=None if accept else RejectionReason.NO_CITATION,
    )


# --------------------------------------------------------------- route_after_planner


def test_route_after_planner_single_chart_fact_routes_to_v1_single() -> None:
    state = initial_state(user_query="q", session={})
    state["sub_queries"] = [_sub_query(claim_type=ClaimType.CHART_FACT)]

    assert route_after_planner(state) == ROUTE_V1_SINGLE


def test_route_after_planner_multi_claim_fans_out() -> None:
    state = initial_state(user_query="q", session={})
    state["sub_queries"] = [
        _sub_query(claim_type=ClaimType.CHART_FACT, sq_id="sq1"),
        _sub_query(claim_type=ClaimType.GUIDELINE, sq_id="sq2"),
    ]

    assert route_after_planner(state) == ROUTE_FAN_OUT


def test_route_after_planner_single_doc_fact_fans_out() -> None:
    state = initial_state(user_query="q", session={})
    state["sub_queries"] = [_sub_query(claim_type=ClaimType.DOC_FACT)]

    assert route_after_planner(state) == ROUTE_FAN_OUT


def test_route_after_planner_single_guideline_fans_out() -> None:
    state = initial_state(user_query="q", session={})
    state["sub_queries"] = [_sub_query(claim_type=ClaimType.GUIDELINE)]

    assert route_after_planner(state) == ROUTE_FAN_OUT


def test_route_after_planner_empty_plan_fans_out() -> None:
    """Planner returned no sub-queries — fan-out so verification can
    surface NO_DATA. Sending an empty plan to v1_single would just
    hide the planner failure."""

    state = initial_state(user_query="q", session={})

    assert route_after_planner(state) == ROUTE_FAN_OUT


# --------------------------------------------------------------- route_after_critic


def test_route_after_critic_all_accepted_routes_to_verification() -> None:
    state = initial_state(user_query="q", session={})
    state["verdicts"] = [
        _verdict(sq_id="sq1", accept=True),
        _verdict(sq_id="sq2", accept=True),
    ]

    assert route_after_critic(state) == ROUTE_VERIFICATION


def test_route_after_critic_rejection_with_budget_routes_to_retry() -> None:
    state = initial_state(user_query="q", session={})
    state["verdicts"] = [_verdict(sq_id="sq1", accept=False)]
    # retry_counts implicitly 0 → budget remaining

    assert route_after_critic(state) == ROUTE_RETRY


def test_route_after_critic_rejection_without_budget_routes_to_abstain() -> None:
    state = initial_state(user_query="q", session={})
    state["verdicts"] = [_verdict(sq_id="sq1", accept=False)]
    state["retry_counts"] = {"sq1": 1}  # budget exhausted

    assert route_after_critic(state) == ROUTE_ABSTAIN


def test_route_after_critic_mixed_one_retryable_one_exhausted_prefers_retry() -> None:
    """If any sub-query still has retry budget, prefer retry over
    abstain — exhausted ones flow through verification next round."""

    state = initial_state(user_query="q", session={})
    state["verdicts"] = [
        _verdict(sq_id="sq1", accept=False),
        _verdict(sq_id="sq2", accept=False),
    ]
    state["retry_counts"] = {"sq1": 1, "sq2": 0}  # sq1 exhausted, sq2 retryable

    assert route_after_critic(state) == ROUTE_RETRY


def test_route_after_critic_no_verdicts_routes_to_verification() -> None:
    """Empty verdicts means no draft was judged — verification surfaces
    NO_DATA. Same code path as 'all accepted' because no useful retry
    can run."""

    state = initial_state(user_query="q", session={})

    assert route_after_critic(state) == ROUTE_VERIFICATION


@pytest.mark.parametrize(
    ("accept_a", "accept_b", "expected"),
    [
        (True, True, ROUTE_VERIFICATION),
        (True, False, ROUTE_RETRY),
        (False, False, ROUTE_RETRY),
    ],
)
def test_route_after_critic_parametrized(
    *, accept_a: bool, accept_b: bool, expected: str,
) -> None:
    state = initial_state(user_query="q", session={})
    state["verdicts"] = [
        _verdict(sq_id="sqA", accept=accept_a),
        _verdict(sq_id="sqB", accept=accept_b),
    ]

    assert route_after_critic(state) == expected
