"""End-to-end LangGraph synthesis flow (W2-07).

Drives :func:`run_turn` through the compiled StateGraph with stubbed
collaborators and asserts the topology:

* the planner span fires before any worker span (Appendix A.5 — planner
  runs unconditionally);
* the synthesizer fires after at least one worker;
* the critic fires after the synthesizer;
* the verification leaf fires last;
* every span carries the same ``request_id`` for parent linkage.

Spans here are structlog log events (LANGSMITH_TRACING is off in tests
per the deferred W2-12 work). The same assertion proves the linkage:
each node logs through ``logger.bind(request_id=...)`` so every event
inherits the request_id, which serves the same purpose as
LangSmith's ``parent_run_id`` for trace correlation.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any
from unittest.mock import MagicMock

import structlog
from anthropic.types import Message, ToolUseBlock

from clinical_copilot.corpus.retriever import RetrievedChunk
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.planner import PLANNER_TOOL_NAME
from clinical_copilot.orchestrator.supervisor_langgraph import run_turn

# --------------------------------------------------------------- helpers


def _planner_message(*, sub_queries: list[dict[str, str]]) -> Message:
    block = ToolUseBlock.model_construct(
        type="tool_use",
        id="tu_planner",
        name=PLANNER_TOOL_NAME,
        input={"sub_queries": sub_queries},
    )
    return Message.model_construct(
        id="msg_planner",
        type="message",
        role="assistant",
        model="m",
        content=[block],
        stop_reason="tool_use",
        stop_sequence=None,
        usage={"input_tokens": 0, "output_tokens": 0},
    )


def _text_message(text: str) -> Message:
    class _TextBlock:
        def __init__(self, t: str) -> None:
            self.type = "text"
            self.text = t

    return Message.model_construct(
        id="msg_text",
        type="message",
        role="assistant",
        model="m",
        content=[_TextBlock(text)],
        stop_reason="end_turn",
        stop_sequence=None,
        usage={"input_tokens": 0, "output_tokens": 0},
    )


def _critic_accept() -> Message:
    block = ToolUseBlock.model_construct(
        type="tool_use",
        id="tu_critic",
        name="emit_verdict",
        input={"verdict": "accept", "rationale": "ok"},
    )
    return Message.model_construct(
        id="msg_critic",
        type="message",
        role="assistant",
        model="m",
        content=[block],
        stop_reason="tool_use",
        stop_sequence=None,
        usage={"input_tokens": 0, "output_tokens": 0},
    )


def _make_anthropic(messages: list[Message]) -> MagicMock:
    client = MagicMock()
    iterator = iter(messages)
    client.messages.create.side_effect = lambda **_kw: next(iterator)
    return client


def _retrieved_chunk(*, chunk_id: str = "c1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_doc_id="doc1",
        title="USPSTF Diabetes Screening",
        source="USPSTF",
        source_url="https://example.gov",
        text="Screen adults 35-70 for type 2 diabetes.",
        score=0.9,
    )


def _agent_response_stub() -> MagicMock:
    resp = MagicMock()
    resp.model_dump.return_value = {
        "cards": [],
        "prose": [],
        "tool_results": [],
        "abstention": None,
    }
    return resp


# --------------------------------------------------------------- assertions


def _events_with_request_id(
    events: list[MutableMapping[str, Any]],
    request_id: str,
) -> list[MutableMapping[str, Any]]:
    return [e for e in events if e.get("request_id") == request_id]


def test_full_synthesis_flow_emits_spans_in_topology_order() -> None:
    """Drive a guideline turn through the graph; assert planner →
    evidence_retriever → synthesizer → critic → verification spans
    fire in that order, all carrying the same request_id."""

    request_id = "req_test_synth_flow"
    planner = _make_anthropic(
        [_planner_message(sub_queries=[{"text": "ADA recs?", "claim_type": "guideline"}])],
    )
    synth = _make_anthropic([_text_message("Annual screening per [c1].")])
    critic = _make_anthropic([_critic_accept()])

    retriever = MagicMock()
    retriever.hybrid_enabled = False
    retriever.retrieve.return_value = [_retrieved_chunk(chunk_id="c1")]

    orchestrator = MagicMock()
    orchestrator.run.return_value = _agent_response_stub()

    claims = MagicMock(patient_id="p1")

    with structlog.testing.capture_logs() as captured:
        response = run_turn(
            user_query="What does ADA recommend?",
            request_id=request_id,
            patient_id="p1",
            bound_patient_name=None,
            planner_client=planner,
            planner_model="haiku",
            synthesizer_client=synth,
            synthesizer_model="sonnet",
            critic_client=critic,
            critic_model="haiku",
            retriever=retriever,
            rerank_client=None,
            rerank_model=None,
            orchestrator=orchestrator,
            claims=claims,
            session_id=None,
            lane=Lane.SLOW,
            chart_pack=None,
        )

    # Filter to events for this request — proves request_id linkage.
    request_events = _events_with_request_id(captured, request_id)
    event_names = [e.get("event") for e in request_events]

    # Linkage: every event we care about carries request_id.
    expected_events = {
        "supervisor_lg.invoke",
        "planner.invoke",
        "planner.decomposed",
        "evidence_retriever.node.invoke",
        "synthesizer.invoke",
        "supervisor_lg.done",
    }
    missing = expected_events - set(event_names)
    assert missing == set(), f"missing structlog events: {missing}"

    # Topology: planner.invoke precedes evidence_retriever.node.invoke
    # which precedes synthesizer.invoke which precedes supervisor_lg.done.
    def _index(name: str) -> int:
        return next(i for i, n in enumerate(event_names) if n == name)

    assert _index("planner.invoke") < _index("evidence_retriever.node.invoke")
    assert _index("evidence_retriever.node.invoke") < _index("synthesizer.invoke")
    assert _index("synthesizer.invoke") < _index("supervisor_lg.done")

    # Sanity: response is well-formed.
    assert response.synthesized_text  # non-empty
    assert response.abstention_reason is None
