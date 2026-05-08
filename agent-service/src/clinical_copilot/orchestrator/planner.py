"""Planner node — decomposes the user query into typed sub-queries.

Per PRD2 §5.1 / Appendix A.5 the planner is the *only* place a
``claim_type`` is assigned to a sub-query. It runs unconditionally on
every turn (the §4.5 short-circuit is a *post*-planner edge), and the
worker mapping is fixed in code via :data:`CLAIM_TYPE_TO_WORKER` so
the planner LLM never sees worker names.

Implementation choices:

* **Fast tier model** (Haiku-class). Decomposition is cheap; the
  Anthropic call dominates latency on the planner span. The default
  comes from ``Settings.model_fast`` so an env var override moves
  this without a code change.
* **Structured output via tool_use**. The planner is exposed a single
  tool, ``emit_plan``, whose ``input_schema`` matches the
  :class:`PlannerOutput` Pydantic model below. The model returns a
  single tool_use block; we parse it into :class:`SubQuery` instances
  and write them to ``state["sub_queries"]``.
* **Fail-soft**. If the model emits malformed JSON, no tool call, or
  more than ``MAX_SUB_QUERIES`` items, we log and write an empty
  ``sub_queries`` list. :func:`route_after_planner` then fans out (so
  the verification leaf can collapse to NO_DATA), keeping the graph's
  topology unconditional.
"""

from __future__ import annotations

import json
import uuid
from importlib import resources
from typing import Any, Final, cast

import structlog
from anthropic import Anthropic
from anthropic.types import Message, MessageParam, ToolParam, ToolUseBlock
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clinical_copilot.orchestrator.state import (
    CLAIM_TYPE_TO_WORKER,
    ClaimType,
    SubQuery,
    TurnState,
)

logger = structlog.get_logger(__name__)


MAX_SUB_QUERIES: Final[int] = 4
"""Hard cap; planner prompt also requests <= 4. Keeps slow-lane fan-out
bounded (§4.5 latency budget)."""

PLANNER_TOOL_NAME: Final[str] = "emit_plan"

DEFAULT_MAX_TOKENS: Final[int] = 512


# --------------------------------------------------------------- output


class PlannerSubQueryOutput(BaseModel):
    """Single sub-query as the planner emits it (no id, no worker)."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=400)
    claim_type: ClaimType


class PlannerOutput(BaseModel):
    """Top-level planner response shape."""

    model_config = ConfigDict(frozen=True)

    sub_queries: list[PlannerSubQueryOutput] = Field(
        default_factory=list,
        max_length=MAX_SUB_QUERIES,
    )


# --------------------------------------------------------------- prompt + tool schema


def _system_prompt() -> str:
    """Read the planner system prompt from the bundled .txt file.

    Kept on disk so prompt edits don't require rebuilding any Docker
    image and so reviewers can see the prompt verbatim in the diff.
    """

    return (
        resources.files("clinical_copilot.orchestrator.prompts")
        .joinpath("planner.txt")
        .read_text(encoding="utf-8")
    )


def _tool_schema() -> ToolParam:
    return cast(
        ToolParam,
        {
            "name": PLANNER_TOOL_NAME,
            "description": (
                "Emit the structured plan for the user's query as a list "
                "of typed sub-queries."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sub_queries": {
                        "type": "array",
                        "maxItems": MAX_SUB_QUERIES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                },
                                "claim_type": {
                                    "type": "string",
                                    "enum": [c.value for c in ClaimType],
                                },
                            },
                            "required": ["text", "claim_type"],
                        },
                    },
                },
                "required": ["sub_queries"],
            },
        },
    )


# --------------------------------------------------------------- node body


def plan(
    *,
    client: Anthropic,
    model: str,
    user_query: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    request_id: str | None = None,
) -> list[SubQuery]:
    """Run one planner Anthropic round-trip and return typed SubQuery list.

    Pure of state mutation — :func:`make_node` wraps this for LangGraph.
    Exposed separately so unit tests can hit the parser path with a
    mock Anthropic client without going through the LangGraph runtime.
    """

    log = logger.bind(request_id=request_id, query_len=len(user_query))
    log.info("planner.invoke", model=model)

    messages: list[MessageParam] = [{"role": "user", "content": user_query}]
    tools: list[ToolParam] = [_tool_schema()]

    response: Message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_system_prompt(),
        tools=tools,
        tool_choice={"type": "tool", "name": PLANNER_TOOL_NAME},
        messages=messages,
    )

    tool_use = next(
        (b for b in response.content if isinstance(b, ToolUseBlock)),
        None,
    )
    if tool_use is None:
        log.warning("planner.no_tool_use", reason="no_tool_use_block")
        return []

    raw_input = tool_use.input
    # ``Anthropic`` SDK gives us a parsed dict already; coerce defensively
    # in case it's bytes or a json string in some future SDK rev.
    if isinstance(raw_input, str | bytes | bytearray):
        try:
            raw_input = json.loads(raw_input)
        except json.JSONDecodeError as exc:
            log.warning("planner.tool_input_invalid_json", error=str(exc))
            return []

    try:
        parsed = PlannerOutput.model_validate(raw_input)
    except ValidationError as exc:
        log.warning("planner.tool_input_validation_failed", error=str(exc))
        return []

    sub_queries: list[SubQuery] = [
        SubQuery(
            id=uuid.uuid4().hex,
            text=item.text,
            claim_type=item.claim_type,
            target_worker=CLAIM_TYPE_TO_WORKER[item.claim_type],
        )
        for item in parsed.sub_queries
    ]

    log.info(
        "planner.decomposed",
        count=len(sub_queries),
        claim_types=[sq.claim_type.value for sq in sub_queries],
    )
    return sub_queries


def make_node(
    *,
    client: Anthropic,
    model: str,
) -> Any:
    """Bind the planner to an Anthropic client / model and return a
    LangGraph node body.

    LangGraph nodes receive the full :class:`TurnState` and return a
    partial dict that gets merged in. The node is kept thin — the
    actual planner logic lives in :func:`plan` so unit tests can hit
    it without a running graph.
    """

    def node(state: TurnState) -> dict[str, Any]:
        session = state.get("session", {})
        request_id = session.get("request_id")
        sub_queries = plan(
            client=client,
            model=model,
            user_query=state.get("user_query", ""),
            request_id=request_id,
        )
        return {"sub_queries": sub_queries}

    return node
