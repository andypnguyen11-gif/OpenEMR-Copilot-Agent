"""Compiled-graph tests for the LangGraph supervisor (W2-07).

Includes a ``usage_totals`` regression pin: PR W2-04 was bitten by a
``state.get("usage_totals")`` typo (initial state, always zero) where
``final_state.get(...)`` was meant. The pin scripts non-zero
``response.usage`` on the planner + synthesizer messages and asserts
the run_turn output sums them — a future regression to the same typo
class will fail this test.

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


def _planner_message(
    *,
    sub_queries: list[dict[str, str]],
    usage_in: int = 0,
    usage_out: int = 0,
) -> Message:
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
        usage={"input_tokens": usage_in, "output_tokens": usage_out},
    )


def _text_message(
    text: str,
    *,
    usage_in: int = 0,
    usage_out: int = 0,
) -> Message:
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
        usage={"input_tokens": usage_in, "output_tokens": usage_out},
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


def test_single_chart_fact_no_chart_pack_abstains_no_data() -> None:
    """A single-CHART_FACT sub-query without a chart pack should
    abstain with NO_DATA — there's no chart context to ground the
    answer in. (The §4.5 short-circuit to v1_single is wired in the
    ``route_after_planner`` predicate but currently deferred at the
    topology level; see the planner-router docstring for why.)
    """

    planner_client = _make_anthropic_returning(
        [
            _planner_message(
                sub_queries=[{"text": "Most recent A1c?", "claim_type": "chart_fact"}],
            ),
        ],
    )
    # Synthesizer should not run on the no-pack / no-drafts path; if
    # it does the test fails because the mock raises.
    synth_client = MagicMock()
    synth_client.messages.create.side_effect = AssertionError(
        "synthesizer must not run when there is nothing to ground synthesis on",
    )
    critic_client = MagicMock()
    critic_client.messages.create.side_effect = AssertionError(
        "critic must not run when no usable drafts produced",
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
    assert response.abstention_reason == RuntimeAbstainReason.NO_DATA.value


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


# Critic-rejection-collapses-to-VERIFICATION_FAILED test removed:
# the W2-07 critic gates the LLM-judge stage off by default (see
# critic.judge ``run_llm_judge=False``), so the LLM-rejection path
# this test exercised is unreachable in the early-submission build.
# The deterministic-rejection paths (NO_CITATION, CITATION_TYPE_MISMATCH,
# ACTION_BLACKLIST, CONFIDENCE_FLOOR) are covered in test_critic.py.
# The whole-answer abstain on critic rejection is covered by the
# verification node's logic in this file's empty-plan test below.


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


# ---------------------------------------------------- handoff-anchor regression


def test_guideline_handoffs_surface_chunk_anchor_for_adapter() -> None:
    """Regression: LangGraph supervisor must populate handoffs with the
    citation metadata the wire adapter needs.

    Bug: supervisor_langgraph.py returned ``handoffs=()`` regardless of
    the LangGraph state's actual handoffs. The wire adapter
    (``_supervisor_to_agent_response``) then could not anchor the
    synthesized prose unless it happened to substring-match a chart-pack
    record, and silently rewrote the response to NO_DATA. Reproduced on
    prod 2026-05-09 (request_id 9253100ddddf...): supervisor synthesized
    1056 chars of grounded guideline prose, adapter discarded it.
    """

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
        request_id="req_handoff_repro",
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

    assert response.abstention_reason is None
    assert response.synthesized_text  # non-empty
    assert len(response.handoffs) >= 1, (
        "LangGraph supervisor returned empty handoffs; the wire adapter "
        "cannot anchor synthesized prose without them"
    )

    def _has_anchor(handoff_output: object) -> bool:
        if not isinstance(handoff_output, dict):
            return False
        chunks = handoff_output.get("chunks") or []
        if any(isinstance(c, dict) and c.get("chunk_id") for c in chunks):
            return True
        citations = handoff_output.get("citations") or []
        return any(
            isinstance(c, dict)
            and (
                c.get("source_doc_id")
                or c.get("corpus_id")
                or c.get("source_id")
            )
            for c in citations
        )

    assert any(_has_anchor(h.output) for h in response.handoffs), (
        "Handoffs present but no recognizable citation anchor in any output"
    )


def test_guideline_wire_response_anchors_with_no_chart_pack() -> None:
    """End-to-end regression: LangGraph supervisor → wire adapter must
    surface synthesized prose, not NO_DATA, when no chart_pack is
    supplied. Before the fix the adapter discarded the answer because
    pass 2 (chart-substring match) could not run and pass 1 saw empty
    handoffs.
    """

    from clinical_copilot.main import _supervisor_to_agent_response

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

    sup_response = run_turn(
        user_query="What does ADA recommend?",
        request_id="req_anchor_e2e",
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

    wire = _supervisor_to_agent_response(
        sup_response,
        session_id="session_test",
        chart_pack=None,
    )

    assert wire.abstention is None, (
        f"Expected non-abstention; got {wire.abstention}. "
        "The adapter discarded the synthesized prose because no anchor "
        "was found in handoffs and no chart_pack was available."
    )
    assert len(wire.prose) >= 1
    assert wire.prose[0].source_id, "anchor source_id must be populated"
    assert wire.prose[0].text, "synthesized prose must be preserved"


# ----------------------------------------------------- usage_totals regression


def test_run_turn_aggregates_usage_totals_from_planner_and_synthesizer() -> None:
    """Regression pin for the PR W2-04 ``state.get`` typo.

    ``run_turn`` previously read ``state.get("usage_totals")`` (the
    pre-graph initial state, always zero) where ``final_state.get(...)``
    was meant. The bug only surfaced once the trace writer started
    consuming :attr:`SupervisorResponse.usage_totals` — earlier tests
    didn't assert on it, so the typo lived. Pin: script non-zero
    ``response.usage`` on the planner + synthesizer messages and
    require the run_turn output to sum them. The critic stays on the
    deterministic-only path (``run_llm_judge=False``) so its scripted
    usage is intentionally not folded.
    """

    planner_client = _make_anthropic_returning(
        [
            _planner_message(
                sub_queries=[{"text": "ADA recs?", "claim_type": "guideline"}],
                usage_in=100,
                usage_out=20,
            ),
        ],
    )
    synth_client = _make_anthropic_returning(
        [
            _text_message(
                "The guideline recommends annual screening [c1].",
                usage_in=300,
                usage_out=50,
            ),
        ],
    )
    critic_client = _make_anthropic_returning([_critic_verdict_message(accept=True)])
    retriever = _make_retriever_returning([_retrieved_chunk(chunk_id="c1")])

    response = run_turn(
        user_query="What does ADA recommend?",
        request_id="req_usage",
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

    assert response.usage_totals.input_tokens == 400, (
        f"Expected 100 (planner) + 300 (synthesizer) = 400 input tokens "
        f"folded into the supervisor response; got "
        f"{response.usage_totals.input_tokens}. The most likely cause is "
        f"reading from the pre-graph initial state instead of "
        f"``final_state`` after ``graph.invoke``."
    )
    assert response.usage_totals.output_tokens == 70
