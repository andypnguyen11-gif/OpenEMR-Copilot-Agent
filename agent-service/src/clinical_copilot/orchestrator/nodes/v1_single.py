"""``v1_single`` LangGraph leaf node — wraps the v1 single-loop orchestrator.

This is the §4.5 short-circuit target. When the planner emits exactly
one CHART_FACT sub-query, :func:`route_after_planner` routes here
instead of through the worker fan-out + critic; the v1 Orchestrator
already handles single-claim chart questions efficiently and the
fan-out + critic round-trip would just add latency without changing
the answer.

The node calls :meth:`Orchestrator.run` and writes the resulting
:class:`AgentResponse` straight into ``state["final_response"]`` as a
JSON-serializable dict. The graph then routes from here directly to
``END`` — verification is bypassed because the v1 path already runs
its own verification middleware on the way out.

Implementation note: the v1 Orchestrator is sync and bound to the
process-wide :class:`Settings` via dependency injection at app start.
The node body holds a closure over the orchestrator + the per-request
``claims`` / ``request_id`` / ``session_id`` / ``lane`` /
``bound_patient_name`` (passed in via the session info dict on the
state). This keeps the LangGraph node signature
``state -> partial dict`` while letting the wrapper feed the
orchestrator the same arguments the FastAPI route builds for it.
"""

from __future__ import annotations

from typing import Any

import structlog

from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.state import TurnState

logger = structlog.get_logger(__name__)


def make_node(
    *,
    orchestrator: Orchestrator,
    claims: ClinicianClaims,
    session_id: str | None,
    lane: Lane,
    bound_patient_name: str | None,
) -> Any:
    """Bind per-request context and return a LangGraph node body.

    The node is request-scoped — bind once per ``query_route`` call,
    invoke the compiled graph, drop. This mirrors how the FastAPI
    route already constructs per-request collaborators (the
    cross-patient guard, the chart pack) and keeps the v1
    Orchestrator's per-call audit / metrics hooks intact.
    """

    def node(state: TurnState) -> dict[str, Any]:
        session = state.get("session", {})
        request_id = session.get("request_id", "")
        log = logger.bind(request_id=request_id)
        log.info("v1_single.node.invoke", lane=lane.value)

        response = orchestrator.run(
            query=state.get("user_query", ""),
            claims=claims,
            request_id=request_id,
            session_id=session_id,
            lane=lane,
            bound_patient_name=bound_patient_name,
        )
        # ``AgentResponse`` is a frozen pydantic model with the exact
        # shape :func:`main._supervisor_to_agent_response` would otherwise
        # build. Serialize once so verification can pass it through.
        return {"final_response": response.model_dump(mode="json")}

    return node
