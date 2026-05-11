"""Tests for the redacting LangChain tracer dispatch.

Coverage:
- Supervisor-node spans route to ``redact_supervisor_node_*`` (which
  the existing test_supervisor_node_redaction.py exercises end-to-end —
  here we just verify the right redactor is selected).
- Unknown chain spans (LangGraph plumbing like ``ChannelWrite``,
  ``_planner_router``, the top-level ``LangGraph`` span) are dropped to
  ``{"redacted": True}``.
- ``llm`` and ``tool`` runs are pass-through (their own ``@traceable``
  wrappers handle redaction; double-redacting would strip the safe
  surface needed for cost / token columns).
- Unknown ``run_type`` defaults to strict drop.
- The ``_apply_redaction`` method survives a redactor that raises by
  failing closed (drop) rather than re-raising — a redactor crash must
  never leak the raw payload.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langsmith.schemas import Run

from clinical_copilot.observability.redacting_tracer import (
    RedactingLangChainTracer,
    _pick_redactors,
    _strict_drop,
)


def _build_run(
    *,
    run_type: str,
    name: str,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
) -> Run:
    """Build a minimal Run for testing — only the fields the dispatch reads."""

    return Run(
        id=uuid4(),
        name=name,
        run_type=run_type,
        start_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        inputs=inputs or {},
        outputs=outputs,
        extra={},
    )


@pytest.mark.parametrize(
    "node_name",
    [
        "planner",
        "v1_single",
        "intake_extractor",
        "evidence_retriever",
        "synthesizer",
        "critic",
        "verification",
    ],
)
def test_supervisor_nodes_route_to_supervisor_redactor(node_name: str) -> None:
    in_redactor, out_redactor = _pick_redactors("chain", node_name)
    assert in_redactor is not None and in_redactor.__name__ == "redact_supervisor_node_inputs"
    assert out_redactor is not None and out_redactor.__name__ == "redact_supervisor_node_outputs"


@pytest.mark.parametrize(
    "name",
    [
        "ChannelWrite<...,planner>",
        "_planner_router",
        "_critic_router",
        "LangGraph",
        "RunnableSequence",
    ],
)
def test_unknown_chain_runs_drop_strictly(name: str) -> None:
    in_redactor, out_redactor = _pick_redactors("chain", name)
    assert in_redactor is _strict_drop
    assert out_redactor is _strict_drop


def test_llm_and_tool_runs_are_passthrough() -> None:
    # @traceable wrappers handle these; double-redacting would strip
    # usage_metadata / model name needed for cost computation.
    assert _pick_redactors("llm", "llm.complete") == (None, None)
    assert _pick_redactors("tool", "tool.dispatch") == (None, None)


def test_unknown_run_type_drops_strictly() -> None:
    in_redactor, out_redactor = _pick_redactors("retriever", "anything")
    assert in_redactor is _strict_drop
    assert out_redactor is _strict_drop


def test_apply_redaction_strips_phi_from_supervisor_node_run() -> None:
    """End-to-end: a synthesizer run carrying a raw TurnState in inputs
    and a raw return dict in outputs should land scrubbed."""

    raw_state = {
        "user_query": "What's Olivia's most recent TSH?",
        "session": {
            "request_id": "r-1",
            "patient_id": "patient-uuid-here",
            "patient_name": "Olivia Smith",
            "history": [],
        },
        "drafts": [
            {"text": "Hashimoto's thyroiditis ...", "abstain_reason": None},
        ],
        "sub_queries": [],
        "verdicts": [],
    }
    raw_return = {
        "final_response": {
            "synthesized_text": "Patient Olivia has TSH 8.4 mIU/L (Hashimoto's).",
            "abstention_reason": None,
            "handoffs": [],
            "iterations": 1,
        },
        "usage_totals": {"input_tokens": 100, "output_tokens": 50},
    }
    run = _build_run(
        run_type="chain",
        name="synthesizer",
        inputs={"state": raw_state},
        outputs=raw_return,
    )

    tracer = RedactingLangChainTracer.__new__(RedactingLangChainTracer)
    tracer._apply_redaction(run)

    flat_in = repr(run.inputs)
    flat_out = repr(run.outputs)
    for sentinel in ["Olivia", "TSH", "Hashimoto", "patient-uuid-here"]:
        assert sentinel not in flat_in, f"{sentinel} survived in inputs: {flat_in}"
        assert sentinel not in flat_out, f"{sentinel} survived in outputs: {flat_out}"
    # Sanity: the redacted shape preserves the safe keys.
    assert run.inputs.get("user_query_length") == len(raw_state["user_query"])
    assert run.outputs.get("synthesized_text_length") == len(
        raw_return["final_response"]["synthesized_text"]
    )


def test_apply_redaction_drops_unknown_chain_run() -> None:
    raw_state = {"user_query": "Olivia ...", "session": {"patient_name": "Olivia"}}
    run = _build_run(
        run_type="chain",
        name="ChannelWrite<...,planner>",
        inputs=raw_state,
        outputs=raw_state,
    )
    tracer = RedactingLangChainTracer.__new__(RedactingLangChainTracer)
    tracer._apply_redaction(run)

    assert run.inputs == {"redacted": True}
    assert run.outputs == {"redacted": True}


def test_apply_redaction_passes_through_llm_run() -> None:
    """LLM spans must NOT be re-redacted — the @traceable wrapper already
    emitted the safe surface (usage_metadata, model name, tool_use_names)
    and re-running a redactor here would strip it."""

    safe_payload = {
        "model": "claude-sonnet-4-6",
        "message_count": 3,
        "system_prompt_length": 1234,
    }
    safe_output = {
        "stop_reason": "end_turn",
        "text_length": 540,
        "usage_metadata": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
    }
    run = _build_run(
        run_type="llm",
        name="llm.complete",
        inputs=safe_payload,
        outputs=safe_output,
    )
    tracer = RedactingLangChainTracer.__new__(RedactingLangChainTracer)
    tracer._apply_redaction(run)

    assert run.inputs == safe_payload  # unchanged
    assert run.outputs == safe_output  # unchanged


def test_apply_redaction_fails_closed_on_redactor_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the dispatched redactor raises (unexpected payload shape), the
    payload must be dropped rather than passed through raw."""

    from clinical_copilot.observability import redacting_tracer as mod

    def _boom(_payload: object) -> dict[str, Any]:
        raise RuntimeError("redactor crashed")

    monkeypatch.setattr(mod, "_pick_redactors", lambda _rt, _n: (_boom, _boom))

    run = _build_run(
        run_type="chain",
        name="synthesizer",
        inputs={"user_query": "PHI string"},
        outputs={"synthesized_text": "more PHI"},
    )
    tracer = RedactingLangChainTracer.__new__(RedactingLangChainTracer)
    tracer._apply_redaction(run)

    assert run.inputs == {"redacted": True}
    assert run.outputs == {"redacted": True}
