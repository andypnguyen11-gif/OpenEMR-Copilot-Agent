"""``intake_extractor`` LangGraph node — query-time fact lookup.

W2-07 spec calls for this node to wrap ``tools/extracted_facts.py``
(W2-03 query-time read of the agent-db ``extracted_facts`` table).
That table is **not** in the current schema — extracted facts persist
to ``data/extracted/<id>.json`` on the agent-service local disk per
the demo-cut decision in TASKS2 ``W2-03``. A query-time read against
the JSON sidecar is feasible but blurs the durability boundary the
submission narrative is taking ("chart tables are durable storage,
JSON sidecar is a temp buffer between extract and review").

For early-submission the node abstains every doc-fact sub-query with
:data:`RuntimeAbstainReason.NO_DATA`. The downstream verification
node converts that into the user-visible "we don't have facts for
this document yet" copy. This keeps the LangGraph topology honest
(the planner's doc_fact branch is reachable, the critic's per-draft
verdict path is reachable, eval cases that hit doc_fact see the same
shape they will once the real lookup lands) without conflating the
W2-07 ship with the W2-03 query-time read.

When the W2-03 query-time read does land, swap the abstain block for
a lookup of ``extracted_facts`` by patient_id (or document_id when
the planner extracts one from the user query) and emit Drafts with
``source_id`` citations shaped like ``ExtractedFact/<document_id>``.
"""

from __future__ import annotations

from typing import Any

import structlog

from clinical_copilot.orchestrator.state import (
    Draft,
    TurnState,
    Worker,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

logger = structlog.get_logger(__name__)


def make_node() -> Any:
    """Return a LangGraph node body that abstains every doc_fact draft.

    No external dependencies — keeping the signature parameterless
    means the supervisor wiring doesn't have to invent an ``extracted_facts``
    handle that doesn't exist yet.
    """

    def node(state: TurnState) -> dict[str, list[Draft]]:
        sub_queries = state.get("sub_queries", [])
        targeted = [sq for sq in sub_queries if sq.target_worker is Worker.INTAKE_EXTRACTOR]
        session = state.get("session", {})
        request_id = session.get("request_id")
        log = logger.bind(request_id=request_id, count=len(targeted))
        log.info("intake_extractor.node.invoke")

        drafts: list[Draft] = [
            Draft(
                sub_query_id=sub_query.id,
                worker=Worker.INTAKE_EXTRACTOR,
                text="",
                citations=(),
                abstain_reason=RuntimeAbstainReason.NO_DATA.value,
            )
            for sub_query in targeted
        ]
        return {"drafts": drafts}

    return node
