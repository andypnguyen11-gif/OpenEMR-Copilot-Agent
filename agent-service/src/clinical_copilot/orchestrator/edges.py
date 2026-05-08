"""Pure routing predicates for the LangGraph supervisor (W2-07).

Conditional edges in LangGraph are plain functions that read the
:class:`TurnState` dict and return a string label naming the next node.
Keeping them in their own module — distinct from node bodies that may
call out to LLMs or retrieval — lets us unit-test routing in isolation
and reason about the graph topology by reading one short file.

Two edges live here:

* :func:`route_after_planner` — implements the §4.5 short-circuit. A
  single CHART_FACT sub-query routes to ``v1_single``; everything else
  fans out to the workers.
* :func:`route_after_critic` — implements the Appendix A.6 retry cap.
  Rejected drafts with retry budget left loop back to the worker
  fan-out exactly once; otherwise the graph proceeds to verification
  (which collapses any unaccepted draft to an abstention).

Neither function mutates state; both are deterministic given the input
state. Both are unit-tested against fixture states in
``tests/unit/orchestrator/test_edges.py``.
"""

from __future__ import annotations

from clinical_copilot.orchestrator.state import (
    MAX_RETRIES_PER_SUB_QUERY,
    ClaimType,
    CriticVerdict,
    TurnState,
)

# Edge label names the LangGraph compiler maps to next-node ids in
# ``orchestrator/supervisor.py``. Keeping them as module-level
# constants prevents typos in the edge map registration.

ROUTE_V1_SINGLE = "v1_single"
ROUTE_FAN_OUT = "fan_out"

ROUTE_RETRY = "retry"
ROUTE_VERIFICATION = "verification"
ROUTE_ABSTAIN = "abstain"


def route_after_planner(state: TurnState) -> str:
    """Decide the post-planner branch.

    The §4.5 short-circuit fires when the planner emitted exactly one
    sub-query and that sub-query is a CHART_FACT — i.e. a question that
    needs a single round of chart tooling and nothing else. Composite
    queries, doc-fact queries, and guideline queries all fan out
    through the worker nodes + critic.

    Empty ``sub_queries`` (planner returned nothing) also fans out so
    the verification leaf can collapse to NO_DATA — sending an empty
    plan through ``v1_single`` would just hide the planner failure.
    """

    sub_queries = state.get("sub_queries", [])
    if len(sub_queries) == 1 and sub_queries[0].claim_type is ClaimType.CHART_FACT:
        return ROUTE_V1_SINGLE
    return ROUTE_FAN_OUT


def route_after_critic(state: TurnState) -> str:
    """Decide the post-critic branch.

    Returns one of:

    * :data:`ROUTE_RETRY` — at least one rejected draft has retry
      budget remaining; loop back through the worker fan-out.
    * :data:`ROUTE_ABSTAIN` — at least one sub-query has been rejected
      *and* has exhausted its retries; verification will render the
      remaining accepted drafts and collapse the rejected one(s) to
      VERIFICATION_FAILED per A.6.
    * :data:`ROUTE_VERIFICATION` — every draft accepted; cleanest path.

    The retry-vs-abstain split is the substantive part: per A.6,
    "second rejection of the same sub-query forces abstain on that
    sub-query." The verification node also handles the whole-answer
    abstain triggers (≥50% rejected, action blacklist) — the edge
    function only decides whether more LLM work is worth doing.
    """

    verdicts = state.get("verdicts", [])
    if not verdicts:
        # Nothing to judge — nothing was drafted. Verification will
        # surface this as NO_DATA. Same code path as "all accepted"
        # because there's no useful retry to attempt.
        return ROUTE_VERIFICATION

    retry_counts = state.get("retry_counts", {})
    has_retryable_rejection = False
    has_exhausted_rejection = False
    for verdict in verdicts:
        if verdict.verdict is not CriticVerdict.REJECT:
            continue
        used = retry_counts.get(verdict.sub_query_id, 0)
        if used < MAX_RETRIES_PER_SUB_QUERY:
            has_retryable_rejection = True
        else:
            has_exhausted_rejection = True

    if has_retryable_rejection:
        return ROUTE_RETRY
    if has_exhausted_rejection:
        return ROUTE_ABSTAIN
    return ROUTE_VERIFICATION
