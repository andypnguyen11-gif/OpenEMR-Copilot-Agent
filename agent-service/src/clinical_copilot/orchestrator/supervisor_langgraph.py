"""LangGraph StateGraph supervisor (W2-07).

Sibling of the plain-Python tool_use supervisor in
:mod:`orchestrator.supervisor` — the latter stays as the v1 fallback
when ``Settings.use_langgraph`` is False or the StateGraph wiring is
missing collaborators.

Topology
========

::

    START
      │
      ▼
    ┌─────────┐
    │ planner │    Haiku → typed list[SubQuery]
    └────┬────┘
         │
   route_after_planner
         │
   ┌─────┴─────┐
   │           │ fan_out (Send to both workers in parallel)
   ▼           │
 v1_single  ┌──┴──────────────────┬──────────────────┐
   │        │                     │                  │
   │        ▼                     ▼                  │
   │   evidence_retriever   intake_extractor         │
   │        │                     │                  │
   │        └────────┬────────────┘                  │
   │                 ▼                               │
   │           synthesizer    Sonnet single-call     │
   │                 │                               │
   │                 ▼                               │
   │              critic   per-Draft Verdict (A.6)   │
   │                 │                               │
   │       route_after_critic                        │
   │                 │                               │
   │                 ▼                               │
   │           verification    AgentResponse build   │
   │                 │                               │
   ▼                 ▼                               │
                    END

Single-claim CHART_FACT short-circuits to ``v1_single``; everything
else fans out, gets synthesized once, judged once, verified once.

For early submission scope **retry is wired in the route function but
not in the edge map** — the unit test for :func:`route_after_critic`
asserts the retry branch exists; the StateGraph's runtime path
collapses retry to ``abstain`` so the integration test stays bounded.
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

import structlog
from anthropic import Anthropic
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from langgraph.types import Send

from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.corpus.rerank import CohereRerankClient
from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.chart_pack import ChartPack
from clinical_copilot.orchestrator.critic import make_node as make_critic_node
from clinical_copilot.orchestrator.edges import (
    ROUTE_RETRY,
    ROUTE_VERIFICATION,
    route_after_critic,
)
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.nodes.evidence_retriever import (
    make_node as make_evidence_retriever_node,
)
from clinical_copilot.orchestrator.nodes.intake_extractor import (
    make_node as make_intake_extractor_node,
)
from clinical_copilot.orchestrator.nodes.v1_single import (
    make_node as make_v1_single_node,
)
from clinical_copilot.orchestrator.planner import make_node as make_planner_node
from clinical_copilot.orchestrator.state import (
    Citation,
    CriticVerdict,
    Draft,
    SessionInfo,
    TurnState,
    Worker,
    initial_state,
)
from clinical_copilot.orchestrator.supervisor import (
    SupervisorResponse,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

logger = structlog.get_logger(__name__)


NODE_PLANNER: Final[str] = "planner"
NODE_V1_SINGLE: Final[str] = "v1_single"
NODE_INTAKE_EXTRACTOR: Final[str] = Worker.INTAKE_EXTRACTOR.value
NODE_EVIDENCE_RETRIEVER: Final[str] = Worker.EVIDENCE_RETRIEVER.value
NODE_SYNTHESIZER: Final[str] = "synthesizer"
NODE_CRITIC: Final[str] = "critic"
NODE_VERIFICATION: Final[str] = "verification"


SYNTHESIZER_SYSTEM_PROMPT = """\
You are the synthesizer of a clinical-copilot multi-agent graph. The
worker nodes have already retrieved everything you need. Read each
worker draft and any patient chart records the supervisor pre-fetched,
then write one short answer that the clinician can read.

Hard constraints:

* Every claim must cite either a chart record's source_id (e.g.
  ``Observation/123``) or a guideline corpus_id (e.g. a chunk_id from
  the evidence retriever). Cite verbatim — do not invent ids.
* Never advise a clinical action ("start", "stop", "increase",
  "decrease", "switch to", "discontinue", "recommend X-ing"). The
  critic enforces this; an action verb in your output triggers a
  whole-answer abstain.
* Do not paraphrase guideline language beyond what the retrieved
  chunk literally says. Restate, don't reinterpret.
* If no worker produced a usable draft, return the literal string
  ``ABSTAIN_NO_DATA`` and nothing else.
* Keep the answer under 180 words. Bullet form when there's more than
  one claim.
"""


# --------------------------------------------------------------- synthesizer


def _format_synthesizer_prompt(
    *,
    user_query: str,
    drafts: list[Draft],
    chart_pack: ChartPack | None,
) -> str:
    """Render drafts + chart pack into a single prompt block.

    The chart pack section is omitted when empty so a corpus-only
    turn stays compact. Drafts are listed in the order workers
    appended them — there's no semantic ordering, just a stable
    presentation.
    """

    sections: list[str] = []
    if chart_pack is not None and not chart_pack.is_empty():
        sections.append(chart_pack.to_prompt_block())

    if drafts:
        sections.append("Worker drafts:")
        for draft in drafts:
            citations_payload = [c.model_dump(exclude_none=True) for c in draft.citations]
            sections.append(
                json.dumps(
                    {
                        "sub_query_id": draft.sub_query_id,
                        "worker": draft.worker.value,
                        "text": draft.text,
                        "citations": citations_payload,
                        "abstain_reason": draft.abstain_reason,
                    },
                    default=str,
                ),
            )

    sections.append(f"User question: {user_query}")
    return "\n\n".join(sections)


def _make_synthesizer_node(
    *,
    client: Anthropic,
    model: str,
    chart_pack: ChartPack | None,
) -> Any:
    """Build the synthesizer node body.

    Single Anthropic call. Returns a partial state with a tentative
    ``final_response`` shaped like :class:`SupervisorResponse` (the
    shape :func:`main._supervisor_to_agent_response` expects). The
    critic still gets a crack at each draft afterwards; if the critic
    rejects everything, :func:`_make_verification_node` overwrites the
    tentative response with an abstention.
    """

    def node(state: TurnState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        usable = [d for d in drafts if d.abstain_reason is None]
        session = state.get("session", {})
        request_id = session.get("request_id")
        has_chart_pack = chart_pack is not None and not chart_pack.is_empty()
        log = logger.bind(
            request_id=request_id,
            drafts=len(drafts),
            usable=len(usable),
            has_chart_pack=has_chart_pack,
        )
        log.info("synthesizer.invoke")

        # Only abstain early when there's NOTHING to ground a synthesis on:
        # no usable worker drafts AND no chart pack records. A chart-only
        # question (e.g. "what was Olivia's most recent TSH?") legitimately
        # produces zero drafts (no GUIDELINE / DOC_FACT sub-queries) but
        # the chart pack carries the answer; let the LLM combine the
        # planner's sub-queries against chart records and emit a cited
        # response.
        if not usable and not has_chart_pack:
            return {
                "final_response": {
                    "synthesized_text": "",
                    "abstention_reason": RuntimeAbstainReason.NO_DATA.value,
                    "handoffs": [],
                    "iterations": 0,
                },
            }

        prompt = _format_synthesizer_prompt(
            user_query=state.get("user_query", ""),
            drafts=usable,
            chart_pack=chart_pack,
        )
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=SYNTHESIZER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        )
        if text.strip() == "ABSTAIN_NO_DATA":
            return {
                "final_response": {
                    "synthesized_text": "",
                    "abstention_reason": RuntimeAbstainReason.NO_DATA.value,
                    "handoffs": [],
                    "iterations": 0,
                },
            }

        return {
            "final_response": {
                "synthesized_text": text,
                "abstention_reason": None,
                "handoffs": [_draft_to_handoff(d) for d in drafts],
                "iterations": 1,
            },
        }

    return node


def _draft_to_handoff(draft: Draft) -> dict[str, Any]:
    """Project a Draft into the existing handoff shape so the legacy
    ``_supervisor_to_agent_response`` adapter sees a familiar dict.
    """

    return {
        "worker": draft.worker.value,
        "sub_query_id": draft.sub_query_id,
        "abstain_reason": draft.abstain_reason,
        "citations": [c.model_dump(exclude_none=True) for c in draft.citations],
    }


# --------------------------------------------------------------- verification


def _make_verification_node() -> Any:
    """Build the verification leaf node body.

    The critic has already attached :class:`Verdict` rows for every
    draft. This node enforces the A.6 sentence-level rules at the
    response level:

    * Any rejected draft → whole-answer abstain with VERIFICATION_FAILED.
      (Sentence-level rejection is not yet wired; whole-answer is
      conservative and matches the v1 fast-lane behavior.)
    * No drafts at all → NO_DATA abstain.
    * All accepted → tentative final_response stands.

    Pure of LLM calls — runs in microseconds.
    """

    def node(state: TurnState) -> dict[str, Any]:
        verdicts = state.get("verdicts", [])
        final = state.get("final_response") or {}

        if not verdicts:
            # Either v1_single already wrote final_response (in which
            # case we pass it through) or no draft was produced.
            if final.get("synthesized_text") or final.get("abstention_reason"):
                return {"final_response": final}
            return {
                "final_response": {
                    "synthesized_text": "",
                    "abstention_reason": RuntimeAbstainReason.NO_DATA.value,
                    "handoffs": [],
                    "iterations": 0,
                },
            }

        any_reject = any(v.verdict is CriticVerdict.REJECT for v in verdicts)
        if any_reject:
            return {
                "final_response": {
                    "synthesized_text": "",
                    "abstention_reason": RuntimeAbstainReason.VERIFICATION_FAILED.value,
                    "handoffs": final.get("handoffs", []),
                    "iterations": final.get("iterations", 0),
                },
            }
        return {"final_response": final}

    return node


# --------------------------------------------------------------- routers


def _planner_router(state: TurnState) -> str | list[Send]:
    """Map planner output into a parallel fan-out via Send.

    Returning a list of :class:`Send` is LangGraph 0.2's parallel
    fan-out idiom: each Send schedules an independent invocation of
    the named node with the given (or shared) state. We pass the full
    state because each worker filters by ``target_worker`` internally.

    **§4.5 short-circuit deferred.** The :func:`route_after_planner`
    predicate (and its unit tests) remain accurate — a single
    CHART_FACT sub-query *would* route to ``v1_single`` if we used
    the predicate's verdict. We don't, because the v1 Orchestrator
    returns an :class:`AgentResponse`-shaped result that the
    verification node was misreading as "no synthesized text" and
    overwriting with NO_DATA on the prod smoke. The proper fix is to
    have v1_single bypass verification entirely (the v1 verification
    middleware already ran inside ``Orchestrator.run``); until that
    refactor lands, route every turn through the fan-out so chart-fact
    questions reach the synthesizer with the chart pack in context.
    The synthesizer's chart-pack-aware path (see
    :func:`_make_synthesizer_node`) handles chart-only turns cleanly.
    """

    return [
        Send(NODE_INTAKE_EXTRACTOR, state),
        Send(NODE_EVIDENCE_RETRIEVER, state),
    ]


def _critic_router(state: TurnState) -> str:
    """Map post-critic state to the next concrete node name.

    Retry is folded into the verification path for early submission
    (see module docstring): a real retry edge would require a custom
    reducer to clear drafts/verdicts of the retried sub_query, which
    is W2-08 territory.
    """

    decision = route_after_critic(state)
    if decision == ROUTE_RETRY:
        # Fold to verification — verification will see the rejected
        # verdict and abstain. Future work re-wires this to a
        # cleardown + re-fan-out path.
        return NODE_VERIFICATION
    if decision == ROUTE_VERIFICATION:
        return NODE_VERIFICATION
    return NODE_VERIFICATION  # ROUTE_ABSTAIN


# --------------------------------------------------------------- builder


def build_graph(
    *,
    planner_client: Anthropic,
    planner_model: str,
    synthesizer_client: Anthropic,
    synthesizer_model: str,
    critic_client: Anthropic,
    critic_model: str,
    retriever: CorpusRetriever,
    rerank_client: Anthropic | None,
    rerank_model: str | None,
    cohere_client: CohereRerankClient | None = None,
    cohere_model: str | None = None,
    orchestrator: Orchestrator,
    claims: ClinicianClaims,
    session_id: str | None,
    lane: Lane,
    bound_patient_name: str | None,
    chart_pack: ChartPack | None,
) -> CompiledStateGraph:
    """Build and compile the StateGraph for one turn.

    The compiled graph is request-scoped — :func:`run_turn` builds a
    fresh one per call because :class:`v1_single` binds per-request
    ``claims`` and ``session_id``. The compile cost is sub-millisecond
    so this is fine; if profiling later flags it, switch to building
    the graph once at app start with thread-local request context.
    """

    builder = StateGraph(TurnState)
    builder.add_node(
        NODE_PLANNER,
        make_planner_node(client=planner_client, model=planner_model),
    )
    builder.add_node(
        NODE_V1_SINGLE,
        make_v1_single_node(
            orchestrator=orchestrator,
            claims=claims,
            session_id=session_id,
            lane=lane,
            bound_patient_name=bound_patient_name,
        ),
    )
    builder.add_node(
        NODE_INTAKE_EXTRACTOR,
        make_intake_extractor_node(),
    )
    builder.add_node(
        NODE_EVIDENCE_RETRIEVER,
        make_evidence_retriever_node(
            retriever=retriever,
            rerank_client=rerank_client,
            rerank_model=rerank_model,
            cohere_client=cohere_client,
            cohere_model=cohere_model,
        ),
    )
    builder.add_node(
        NODE_SYNTHESIZER,
        _make_synthesizer_node(
            client=synthesizer_client,
            model=synthesizer_model,
            chart_pack=chart_pack,
        ),
    )
    builder.add_node(
        NODE_CRITIC,
        make_critic_node(client=critic_client, model=critic_model),
    )
    builder.add_node(
        NODE_VERIFICATION,
        _make_verification_node(),
    )

    builder.add_edge(START, NODE_PLANNER)
    builder.add_conditional_edges(
        NODE_PLANNER,
        cast(Any, _planner_router),
        [NODE_V1_SINGLE, NODE_INTAKE_EXTRACTOR, NODE_EVIDENCE_RETRIEVER],
    )
    builder.add_edge(NODE_INTAKE_EXTRACTOR, NODE_SYNTHESIZER)
    builder.add_edge(NODE_EVIDENCE_RETRIEVER, NODE_SYNTHESIZER)
    builder.add_edge(NODE_SYNTHESIZER, NODE_CRITIC)
    builder.add_conditional_edges(
        NODE_CRITIC,
        _critic_router,
        [NODE_VERIFICATION],
    )
    builder.add_edge(NODE_V1_SINGLE, NODE_VERIFICATION)
    builder.add_edge(NODE_VERIFICATION, END)

    return builder.compile()


# --------------------------------------------------------------- public API


def run_turn(
    *,
    user_query: str,
    request_id: str,
    patient_id: str,
    bound_patient_name: str | None,
    planner_client: Anthropic,
    planner_model: str,
    synthesizer_client: Anthropic,
    synthesizer_model: str,
    critic_client: Anthropic,
    critic_model: str,
    retriever: CorpusRetriever,
    rerank_client: Anthropic | None,
    rerank_model: str | None,
    cohere_client: CohereRerankClient | None = None,
    cohere_model: str | None = None,
    orchestrator: Orchestrator,
    claims: ClinicianClaims,
    session_id: str | None,
    lane: Lane,
    chart_pack: ChartPack | None,
) -> SupervisorResponse:
    """Run one turn through the compiled StateGraph.

    Returns the same :class:`SupervisorResponse` shape the plain-Python
    supervisor returns, so :func:`main._supervisor_to_agent_response`
    works for both code paths without branching.
    """

    graph = build_graph(
        planner_client=planner_client,
        planner_model=planner_model,
        synthesizer_client=synthesizer_client,
        synthesizer_model=synthesizer_model,
        critic_client=critic_client,
        critic_model=critic_model,
        retriever=retriever,
        rerank_client=rerank_client,
        rerank_model=rerank_model,
        cohere_client=cohere_client,
        cohere_model=cohere_model,
        orchestrator=orchestrator,
        claims=claims,
        session_id=session_id,
        lane=lane,
        bound_patient_name=bound_patient_name,
        chart_pack=chart_pack,
    )

    session: SessionInfo = SessionInfo(
        request_id=request_id,
        patient_id=patient_id,
        patient_name=bound_patient_name,
        history=[],
    )
    state = initial_state(user_query=user_query, session=session)

    log = logger.bind(request_id=request_id, query_len=len(user_query))
    log.info("supervisor_lg.invoke")
    final_state: TurnState = graph.invoke(state)  # type: ignore[assignment]

    final = final_state.get("final_response") or {}
    if "model_dump" in dir(final):
        # v1_single may have written an AgentResponse-compatible dict
        # via .model_dump; nothing to do.
        pass

    text = str(final.get("synthesized_text", ""))
    abstain = final.get("abstention_reason")
    handoffs_payload = final.get("handoffs", [])
    iterations = int(final.get("iterations", 0) or 0)

    log.info(
        "supervisor_lg.done",
        text_len=len(text),
        abstention=abstain,
        handoffs=len(handoffs_payload) if isinstance(handoffs_payload, list) else 0,
    )

    # Build a SupervisorResponse so :func:`_supervisor_to_agent_response`
    # in main.py works unchanged. Handoffs from the LangGraph path are
    # synthetic (we don't have the same per-tool latency the plain-Python
    # supervisor records) so we project a minimal shape that the
    # existing adapter tolerates.
    return SupervisorResponse(
        synthesized_text=text,
        handoffs=(),  # plain-Python Handoff tuples are heavy; legacy adapter is tolerant of empty
        abstention_reason=abstain if isinstance(abstain, str) else None,
        iterations=iterations,
    )


# Re-export for unit tests that want to construct a Citation directly
# without hopping through state.py.
__all__ = [
    "Citation",
    "build_graph",
    "run_turn",
]
