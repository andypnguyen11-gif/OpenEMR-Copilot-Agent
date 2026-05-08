"""Compiled-graph tests for the LangGraph supervisor (W2-07).

The integration synthesis_flow_test exercises the full route end-to-
end; these unit tests target a tighter contract:

* the graph compiles given a complete wiring;
* a single-CHART_FACT plan exits via ``v1_single`` (the §4.5
  short-circuit) and ``final_response`` mirrors the v1
  AgentResponse;
* a guideline plan flows through ``evidence_retriever`` → synthesizer
  → critic → verification with a typed ``SupervisorResponse`` out;
* a critic-rejected guideline plan flows to verification and
  abstains with VERIFICATION_FAILED (no whole-answer leak).

Anthropic and the corpus retriever are stubbed; no network, no
filesystem reads beyond the prompt files bundled in the source tree.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from anthropic.types import Message, ToolUseBlock

from clinical_copilot.corpus.retriever import RetrievedChunk
from clinical_copilot.orchestrator.planner import PLANNER_TOOL_NAME
from clinical_copilot.orchestrator.supervisor import SupervisorResponse
from clinical_copilot.orchestrator.supervisor_langgraph import run_turn
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

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


def _critic_verdict_message(*, accept: bool) -> Message:
    block = ToolUseBlock.model_construct(
        type="tool_use",
        id="tu_critic",
        name="emit_verdict",
        input=(
            {"verdict": "accept", "rationale": "ok"}
            if accept
            else {
                "verdict": "reject",
                "rejection_reason": "judge_rejected",
                "rationale": "no support",
            }
        ),
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


def _make_anthropic_returning(messages: list[Message]) -> MagicMock:
    """Return a mock Anthropic client whose ``messages.create`` returns
    each message in order across successive calls.
    """

    client = MagicMock()
    iterator = iter(messages)
    client.messages.create.side_effect = lambda **_kw: next(iterator)
    return client


def _make_retriever_returning(chunks: list[RetrievedChunk]) -> MagicMock:
    retriever = MagicMock()
    retriever.hybrid_enabled = False
    retriever.retrieve.return_value = chunks
    return retriever


def _retrieved_chunk(*, chunk_id: str = "c1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_doc_id="doc1",
        title="USPSTF Diabetes Screening",
        source="USPSTF",
        source_url="https://example.gov/uspstf/diabetes",
        text="Screen adults aged 35 to 70 for type 2 diabetes.",
        score=0.9,
    )


def _make_orchestrator_returning(response: Any) -> MagicMock:
    orch = MagicMock()
    orch.run.return_value = response
    return orch


def _claims_stub() -> Any:
    return MagicMock(patient_id="p1")


def _agent_response_stub() -> MagicMock:
    """Stand-in for an :class:`AgentResponse` — the v1_single node
    only calls ``model_dump`` on it, so a mock that returns a sane
    dict is enough for graph-flow tests."""

    resp = MagicMock()
    resp.model_dump.return_value = {
        "cards": [],
        "prose": [{"text": "v1 single answer", "source_id": "Observation/1"}],
        "tool_results": [],
        "abstention": None,
    }
    return resp


# --------------------------------------------------------------- short-circuit


def test_single_chart_fact_short_circuits_to_v1_single() -> None:
    """One CHART_FACT sub-query routes through v1_single, skipping
    fan-out + critic. The compiled graph still runs end-to-end."""

    planner_client = _make_anthropic_returning(
        [
            _planner_message(
                sub_queries=[{"text": "Most recent A1c?", "claim_type": "chart_fact"}],
            ),
        ],
    )
    # synthesizer/critic Anthropic clients should never be invoked on
    # this path; if they are, the test fails with an unexpected mock
    # call.
    synth_client = MagicMock()
    synth_client.messages.create.side_effect = AssertionError(
        "synthesizer must not run on single CHART_FACT short-circuit",
    )
    critic_client = MagicMock()
    critic_client.messages.create.side_effect = AssertionError(
        "critic must not run on single CHART_FACT short-circuit",
    )
    retriever = _make_retriever_returning([])
    orchestrator = _make_orchestrator_returning(_agent_response_stub())

    response = run_turn(
        user_query="What is her current A1c?",
        request_id="req1",
        patient_id="p1",
        bound_patient_name="Jane Doe",
        planner_client=planner_client,
        planner_model="haiku",
        synthesizer_client=synth_client,
        synthesizer_model="sonnet",
        critic_client=critic_client,
        critic_model="haiku",
        retriever=retriever,
        rerank_client=None,
        rerank_model=None,
        orchestrator=orchestrator,
        claims=_claims_stub(),
        session_id=None,
        lane=__import__(
            "clinical_copilot.orchestrator.lanes",
            fromlist=["Lane"],
        ).Lane.SLOW,
        chart_pack=None,
    )

    assert isinstance(response, SupervisorResponse)
    orchestrator.run.assert_called_once()


# --------------------------------------------------------------- guideline path


def test_guideline_plan_flows_through_synthesizer_and_critic() -> None:
    """A single GUIDELINE sub-query fans out → evidence_retriever →
    synthesizer → critic → verification, with all-accept verdict."""

    planner_client = _make_anthropic_returning(
        [
            _planner_message(
                sub_queries=[{"text": "ADA recs?", "claim_type": "guideline"}],
            ),
        ],
    )
    synth_client = _make_anthropic_returning(
        [_text_message("The guideline recommends annual screening [c1].")],
    )
    critic_client = _make_anthropic_returning([_critic_verdict_message(accept=True)])
    retriever = _make_retriever_returning([_retrieved_chunk(chunk_id="c1")])

    response = run_turn(
        user_query="What does ADA recommend?",
        request_id="req2",
        patient_id="p1",
        bound_patient_name=None,
        planner_client=planner_client,
        planner_model="haiku",
        synthesizer_client=synth_client,
        synthesizer_model="sonnet",
        critic_client=critic_client,
        critic_model="haiku",
        retriever=retriever,
        rerank_client=None,
        rerank_model=None,
        orchestrator=_make_orchestrator_returning(_agent_response_stub()),
        claims=_claims_stub(),
        session_id=None,
        lane=__import__(
            "clinical_copilot.orchestrator.lanes",
            fromlist=["Lane"],
        ).Lane.SLOW,
        chart_pack=None,
    )

    assert isinstance(response, SupervisorResponse)
    assert response.abstention_reason is None
    assert response.synthesized_text  # non-empty


def test_critic_rejection_collapses_to_verification_failed_abstain() -> None:
    """Critic rejects the synthesized draft. Verification overwrites
    final_response with VERIFICATION_FAILED — the rejected text never
    reaches the user."""

    planner_client = _make_anthropic_returning(
        [
            _planner_message(
                sub_queries=[{"text": "ADA recs?", "claim_type": "guideline"}],
            ),
        ],
    )
    synth_client = _make_anthropic_returning(
        [_text_message("Hallucinated recommendation with no citation.")],
    )
    critic_client = _make_anthropic_returning([_critic_verdict_message(accept=False)])
    retriever = _make_retriever_returning([_retrieved_chunk(chunk_id="c1")])

    response = run_turn(
        user_query="What does ADA recommend?",
        request_id="req3",
        patient_id="p1",
        bound_patient_name=None,
        planner_client=planner_client,
        planner_model="haiku",
        synthesizer_client=synth_client,
        synthesizer_model="sonnet",
        critic_client=critic_client,
        critic_model="haiku",
        retriever=retriever,
        rerank_client=None,
        rerank_model=None,
        orchestrator=_make_orchestrator_returning(_agent_response_stub()),
        claims=_claims_stub(),
        session_id=None,
        lane=__import__(
            "clinical_copilot.orchestrator.lanes",
            fromlist=["Lane"],
        ).Lane.SLOW,
        chart_pack=None,
    )

    assert response.abstention_reason == RuntimeAbstainReason.VERIFICATION_FAILED.value
    assert response.synthesized_text == ""


# --------------------------------------------------------------- empty plan


def test_empty_planner_plan_collapses_to_no_data_via_verification() -> None:
    """Planner returns no sub-queries; the fan-out path produces no
    drafts; verification writes NO_DATA."""

    planner_client = _make_anthropic_returning([_planner_message(sub_queries=[])])
    # Synthesizer is invoked even on empty drafts; verification then
    # overwrites with NO_DATA.
    synth_client = _make_anthropic_returning([_text_message("anything")])
    critic_client = MagicMock()
    critic_client.messages.create.side_effect = AssertionError(
        "critic must not run when there are no usable drafts",
    )
    retriever = _make_retriever_returning([])

    response = run_turn(
        user_query="totally unrelated",
        request_id="req4",
        patient_id="p1",
        bound_patient_name=None,
        planner_client=planner_client,
        planner_model="haiku",
        synthesizer_client=synth_client,
        synthesizer_model="sonnet",
        critic_client=critic_client,
        critic_model="haiku",
        retriever=retriever,
        rerank_client=None,
        rerank_model=None,
        orchestrator=_make_orchestrator_returning(_agent_response_stub()),
        claims=_claims_stub(),
        session_id=None,
        lane=__import__(
            "clinical_copilot.orchestrator.lanes",
            fromlist=["Lane"],
        ).Lane.SLOW,
        chart_pack=None,
    )

    assert response.abstention_reason == RuntimeAbstainReason.NO_DATA.value
