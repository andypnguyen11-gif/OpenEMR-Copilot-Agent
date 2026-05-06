"""Supervisor with two workers — Week 2 PRD §4 multi-agent graph.

Architecture (early-submission shape, plain Python — no LangGraph):

::

    user query  ──► supervisor.run() ─► Anthropic Messages call with two
                                        tool_use tools defined:
                                          - dispatch_intake_extractor
                                          - dispatch_evidence_retriever

                          The model decides which (if any) to call.
                          A response with text only ends the loop.

                          tool_use → matching worker dispatched → tool
                          result fed back → loop continues until the
                          model returns text or max_iterations is hit.

Every dispatch is recorded in a :class:`Handoff` and emitted via
``structlog`` so the demo can show the handoff log alongside the
chat response. This satisfies the "handoffs must be logged and
explainable" pitfall from the PRD.

Workers are passed in as callables so:

* tests can inject deterministic mocks (no live VLM, no retriever
  index needed);
* the real wiring in ``main.py`` partial-applies a real
  ``intake_extractor`` (with the live Anthropic client + model) and
  a real ``evidence_retriever`` (with a long-lived
  :class:`CorpusRetriever`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

import structlog
from anthropic import Anthropic
from anthropic.types import Message, MessageParam, ToolParam, ToolUseBlock

from clinical_copilot.schemas.abstain import RuntimeAbstainReason

logger = structlog.get_logger(__name__)

WorkerName = Literal["intake_extractor", "evidence_retriever"]

DEFAULT_MAX_ITERATIONS = 4
DEFAULT_MAX_TOKENS = 1024


# --------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class Handoff:
    """One supervisor → worker dispatch + the worker's result.

    Recorded for every tool_use block the supervisor processes,
    successful or not. Latency is measured in milliseconds at the
    supervisor boundary (includes worker run-time but not the
    surrounding Anthropic round-trip).
    """

    worker: WorkerName
    tool_use_id: str
    arguments: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class SupervisorResponse:
    """Final answer assembled by the supervisor."""

    synthesized_text: str
    handoffs: tuple[Handoff, ...]
    abstention_reason: str | None = None
    """One of :class:`RuntimeAbstainReason` when the supervisor refused
    to synthesize. ``None`` when synthesis succeeded."""

    iterations: int = 0
    """How many tool-use turns the model went through. Useful for
    diagnostics; bounded by ``max_iterations``."""


# Worker callables — opaque to the supervisor. They take **kwargs from
# the model's tool input and return a JSON-serializable dict (or raise).
IntakeExtractorFn = Callable[..., dict[str, Any]]
EvidenceRetrieverFn = Callable[..., dict[str, Any]]


# --------------------------------------------------------------- prompts


SYSTEM_PROMPT = """\
You are the supervisor of a small clinical-copilot multi-agent graph.
You have two workers available; pick the right combination for the
user's question and return a synthesized answer.

Workers:
1. dispatch_intake_extractor — when the user references a specific
   document (lab PDF or intake form) you must extract structured facts
   from. Inputs: document_path, document_type ("lab_pdf"|"intake_form").
2. dispatch_evidence_retriever — when the user asks about a guideline,
   recommendation, or supporting evidence. Inputs: query string and an
   optional k (default 5).

Rules:
* You may call workers in either order, both, or only one. Mixed
  questions ("what does the recent lab say AND what guidelines apply?")
  warrant calling both.
* Every claim in your final synthesis must point at a citation from a
  worker's output. If you cannot ground a claim, abstain — do not
  invent.
* Never reveal raw chart text, patient identifiers, or full document
  contents in your synthesis. Summaries with citations only.
* When neither worker is relevant, return an abstention with reason
  NO_DATA.
"""


# --------------------------------------------------------------- tools


def _tool_schemas() -> list[ToolParam]:
    """Two-tool schema the model sees.

    Schemas are deliberately tight: ``document_path`` must be a string,
    ``document_type`` is enum-bound, ``k`` is bounded. Loose schemas
    invite hallucinated argument shapes that the workers then have to
    re-validate at the cost of an extra round trip.
    """

    return [
        cast(
            ToolParam,
            {
                "name": "dispatch_intake_extractor",
                "description": (
                    "Run multimodal extraction on a lab PDF or intake form. "
                    "Returns structured facts with per-field citations."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "document_path": {
                            "type": "string",
                            "description": (
                                "Filesystem path (absolute or relative to "
                                "agent-service/) of the PDF or PNG to extract."
                            ),
                        },
                        "document_type": {
                            "type": "string",
                            "enum": ["lab_pdf", "intake_form"],
                            "description": ("Which schema the extracted facts map to."),
                        },
                        "document_id": {
                            "type": "string",
                            "description": (
                                "Optional stable id for the document. Defaults "
                                "to the file's stem if omitted."
                            ),
                        },
                    },
                    "required": ["document_path", "document_type"],
                },
            },
        ),
        cast(
            ToolParam,
            {
                "name": "dispatch_evidence_retriever",
                "description": (
                    "Run hybrid retrieval (BM25 + optional dense) over the "
                    "guideline corpus. Returns top-k chunks each with a "
                    "source citation."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural-language query to run against the guideline corpus."
                            ),
                        },
                        "k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": 5,
                            "description": "How many chunks to return.",
                        },
                    },
                    "required": ["query"],
                },
            },
        ),
    ]


# --------------------------------------------------------------- main


def run(
    *,
    client: Anthropic,
    model: str,
    query: str,
    intake_extractor: IntakeExtractorFn,
    evidence_retriever: EvidenceRetrieverFn,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    request_id: str | None = None,
) -> SupervisorResponse:
    """Run the supervisor loop. See module docstring for the contract."""

    handoffs: list[Handoff] = []
    messages: list[MessageParam] = [{"role": "user", "content": query}]
    tools = _tool_schemas()
    log = logger.bind(request_id=request_id, query_len=len(query))

    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        log.info("supervisor.turn", iteration=iteration)

        response: Message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        tool_use_blocks = [b for b in response.content if isinstance(b, ToolUseBlock)]
        if not tool_use_blocks:
            text = "".join(
                getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
            )
            log.info(
                "supervisor.synth",
                iterations=iteration,
                handoffs=len(handoffs),
                text_len=len(text),
            )
            return SupervisorResponse(
                synthesized_text=text,
                handoffs=tuple(handoffs),
                iterations=iteration,
            )

        # Append the assistant's tool-use turn before sending tool_result.
        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            handoff, tool_result = _dispatch(
                block=block,
                intake_extractor=intake_extractor,
                evidence_retriever=evidence_retriever,
                log=log,
            )
            handoffs.append(handoff)
            tool_results.append(tool_result)

        messages.append({"role": "user", "content": tool_results})

    # Iteration cap hit — return whatever we have with TOOL_FAILURE.
    log.warning("supervisor.iteration_cap", iterations=iteration)
    return SupervisorResponse(
        synthesized_text="",
        handoffs=tuple(handoffs),
        abstention_reason=RuntimeAbstainReason.TOOL_FAILURE.value,
        iterations=iteration,
    )


def _dispatch(
    *,
    block: ToolUseBlock,
    intake_extractor: IntakeExtractorFn,
    evidence_retriever: EvidenceRetrieverFn,
    log: structlog.stdlib.BoundLogger,
) -> tuple[Handoff, dict[str, Any]]:
    """Dispatch a single tool_use to its worker, time it, log it.

    Returns the handoff record AND the tool_result block to feed back
    into the next supervisor turn.
    """

    args = cast(dict[str, Any], block.input or {})
    started = time.perf_counter()
    output: dict[str, Any] | None
    error: str | None
    is_error: bool
    try:
        if block.name == "dispatch_intake_extractor":
            worker: WorkerName = "intake_extractor"
            output = intake_extractor(**args)
            error = None
            is_error = False
        elif block.name == "dispatch_evidence_retriever":
            worker = "evidence_retriever"
            output = evidence_retriever(**args)
            error = None
            is_error = False
        else:
            worker = cast(WorkerName, block.name)
            output = None
            error = f"unknown tool: {block.name}"
            is_error = True
    except Exception as exc:
        worker = (
            "intake_extractor"
            if block.name == "dispatch_intake_extractor"
            else "evidence_retriever"
        )
        output = None
        error = f"{type(exc).__name__}: {exc}"
        is_error = True

    latency_ms = int((time.perf_counter() - started) * 1000)

    handoff = Handoff(
        worker=worker,
        tool_use_id=block.id,
        arguments=args,
        output=output,
        error=error,
        latency_ms=latency_ms,
    )
    log.info(
        "supervisor.handoff",
        worker=worker,
        tool_use_id=block.id,
        latency_ms=latency_ms,
        is_error=is_error,
    )

    tool_result_payload = (
        json.dumps(output, default=str) if output is not None else error or "no output"
    )
    return handoff, {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": tool_result_payload,
        "is_error": is_error,
    }
