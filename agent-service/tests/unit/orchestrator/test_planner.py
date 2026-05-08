"""Planner node tests (W2-07).

Drives :func:`clinical_copilot.orchestrator.planner.plan` against a
fake Anthropic client whose ``.messages.create`` returns a canned
:class:`anthropic.types.Message` with a single ``ToolUseBlock``. No
real network or API key required.

We assert:

* composite asks decompose to ≥ 2 sub-queries;
* single-claim asks decompose to a 1-element list;
* the LLM picks claim_type and the code (not the LLM) picks
  target_worker via :data:`CLAIM_TYPE_TO_WORKER`;
* malformed planner output abstains to an empty list (so the graph
  fans out and verification can collapse to NO_DATA).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from anthropic.types import Message, ToolUseBlock

from clinical_copilot.orchestrator.planner import (
    PLANNER_TOOL_NAME,
    plan,
)
from clinical_copilot.orchestrator.state import (
    CLAIM_TYPE_TO_WORKER,
    ClaimType,
    Worker,
)

# --------------------------------------------------------------- helpers


def _make_message(*, tool_input: Any) -> Message:
    """Build a real :class:`Message` with one tool_use block.

    ``Message.model_construct`` skips validation so we can pass
    a minimal payload without filling in every required field.
    """

    block = ToolUseBlock.model_construct(
        type="tool_use",
        id="tu_test",
        name=PLANNER_TOOL_NAME,
        input=tool_input,
    )
    return Message.model_construct(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-haiku-4-5-20251001",
        content=[block],
        stop_reason="tool_use",
        stop_sequence=None,
        usage={"input_tokens": 0, "output_tokens": 0},
    )


def _client_returning(message: Message) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = message
    return client


# --------------------------------------------------------------- composite


def test_plan_decomposes_composite_query_into_two_sub_queries() -> None:
    message = _make_message(
        tool_input={
            "sub_queries": [
                {
                    "text": "What is the patient's most recent A1c?",
                    "claim_type": "chart_fact",
                },
                {
                    "text": "What does the ADA recommend for A1c >= 7.0% management?",
                    "claim_type": "guideline",
                },
            ],
        },
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="claude-haiku-4-5-20251001",
        user_query="What's her A1c and what does ADA recommend?",
    )

    assert len(sub_queries) == 2
    assert sub_queries[0].claim_type is ClaimType.CHART_FACT
    assert sub_queries[1].claim_type is ClaimType.GUIDELINE


def test_plan_routing_map_picks_target_worker_not_llm() -> None:
    """The LLM emits claim_type only; CLAIM_TYPE_TO_WORKER assigns
    target_worker. This is the §5.1 split — LLM picks meaning, code
    picks address."""

    message = _make_message(
        tool_input={
            "sub_queries": [
                {"text": "current meds", "claim_type": "chart_fact"},
                {"text": "guideline X", "claim_type": "guideline"},
                {"text": "intake form Y", "claim_type": "doc_fact"},
            ],
        },
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="m",
        user_query="three things",
    )

    assert sub_queries[0].target_worker is CLAIM_TYPE_TO_WORKER[ClaimType.CHART_FACT]
    assert sub_queries[0].target_worker is Worker.CHART_TOOLS
    assert sub_queries[1].target_worker is Worker.EVIDENCE_RETRIEVER
    assert sub_queries[2].target_worker is Worker.INTAKE_EXTRACTOR


# --------------------------------------------------------------- single claim


def test_plan_decomposes_single_claim_into_one_element_list() -> None:
    message = _make_message(
        tool_input={
            "sub_queries": [
                {"text": "What is her current A1c?", "claim_type": "chart_fact"},
            ],
        },
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="m",
        user_query="What's her current A1c?",
    )

    assert len(sub_queries) == 1
    assert sub_queries[0].claim_type is ClaimType.CHART_FACT


# --------------------------------------------------------------- failure paths


def test_plan_returns_empty_list_on_malformed_output() -> None:
    """A planner that returns sub_queries with a bogus claim_type gets
    rejected by Pydantic; the planner abstains to an empty list rather
    than guessing a route."""

    message = _make_message(
        tool_input={
            "sub_queries": [
                {"text": "weird", "claim_type": "not_a_real_claim_type"},
            ],
        },
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="m",
        user_query="weird",
    )

    assert sub_queries == []


def test_plan_returns_empty_list_when_no_tool_use() -> None:
    """If the model emits text only with no tool_use block, the planner
    abstains to an empty list."""

    message = Message.model_construct(
        id="msg_empty",
        type="message",
        role="assistant",
        model="m",
        content=[],
        stop_reason="end_turn",
        stop_sequence=None,
        usage={"input_tokens": 0, "output_tokens": 0},
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="m",
        user_query="hi",
    )

    assert sub_queries == []


def test_plan_assigns_unique_ids_per_sub_query() -> None:
    """Distinct sub-queries get distinct ids — needed so retry_counts
    can index by sub_query_id."""

    message = _make_message(
        tool_input={
            "sub_queries": [
                {"text": "a", "claim_type": "chart_fact"},
                {"text": "b", "claim_type": "guideline"},
                {"text": "c", "claim_type": "doc_fact"},
            ],
        },
    )

    sub_queries = plan(
        client=_client_returning(message),
        model="m",
        user_query="multi",
    )

    ids = {sq.id for sq in sub_queries}
    assert len(ids) == 3
