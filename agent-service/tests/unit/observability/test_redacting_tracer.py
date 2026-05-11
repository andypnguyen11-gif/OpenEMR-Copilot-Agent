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


def test_tool_runs_are_passthrough() -> None:
    # @traceable_tool_dispatch already applies process_inputs/outputs;
    # double-redacting would strip the tool name and record counts.
    assert _pick_redactors("tool", "tool.dispatch") == (None, None)


def test_llm_runs_route_to_llm_redactor() -> None:
    # wrap_anthropic emits llm spans without PHI redaction; we must
    # apply our llm redactor here so PHI in messages/system never
    # reaches LangSmith.
    in_red, out_red = _pick_redactors("llm", "ChatAnthropic")
    assert in_red is not None and in_red.__name__ == "_redact_llm_inputs"
    assert out_red is not None and out_red.__name__ == "_redact_llm_outputs"


def test_llm_redactor_strips_phi_from_wrap_anthropic_shape() -> None:
    """wrap_anthropic emits inputs that match Anthropic's
    .messages.create kwargs and outputs that wrap the Message dict.
    Both shapes carry PHI; the redactor must strip free text but keep
    model name + usage_metadata for cost computation."""

    from clinical_copilot.observability.redacting_tracer import (
        _redact_llm_inputs,
        _redact_llm_outputs,
    )

    raw_inputs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "system": "You are a clinical assistant. Patient: Olivia Smith ...",
        "messages": [
            {"role": "user", "content": "What's Olivia's TSH?"},
            {"role": "assistant", "content": "Hashimoto's confirmed."},
        ],
        "tools": [{"name": "get_observations", "description": "..."}],
    }
    raw_outputs = {
        "output": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Olivia's TSH is 8.4 (Hashimoto's)."}],
        },
        "usage_metadata": {"input_tokens": 1234, "output_tokens": 567, "total_tokens": 1801},
    }

    red_in = _redact_llm_inputs(raw_inputs)
    red_out = _redact_llm_outputs(raw_outputs)

    flat = repr(red_in) + repr(red_out)
    for sentinel in ["Olivia", "Hashimoto", "TSH", "clinical assistant"]:
        assert sentinel not in flat, f"{sentinel} survived: {flat}"
    assert red_in["model"] == "claude-sonnet-4-6"
    assert red_in["message_count"] == 2
    assert red_in["tool_def_names"] == ["get_observations"]
    assert red_out["usage_metadata"] == {
        "input_tokens": 1234,
        "output_tokens": 567,
        "total_tokens": 1801,
    }
    assert red_out["text_length"] == len("Olivia's TSH is 8.4 (Hashimoto's).")


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


def test_apply_redaction_strips_phi_from_llm_run() -> None:
    """LLM spans from wrap_anthropic carry raw messages + system prompts.
    Our llm redactor must strip them while preserving model name and
    usage_metadata so the LangSmith Tokens / Cost columns compute."""

    raw_inputs = {
        "model": "claude-sonnet-4-6",
        "system": "You are a clinical assistant. Olivia ...",
        "messages": [{"role": "user", "content": "Olivia's TSH?"}],
    }
    raw_outputs = {
        "output": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Olivia has Hashimoto's."}],
        },
        "usage_metadata": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
    }
    run = _build_run(
        run_type="llm",
        name="ChatAnthropic",
        inputs=raw_inputs,
        outputs=raw_outputs,
    )
    tracer = RedactingLangChainTracer.__new__(RedactingLangChainTracer)
    tracer._apply_redaction(run)

    flat = repr(run.inputs) + repr(run.outputs)
    for sentinel in ["Olivia", "Hashimoto", "clinical assistant"]:
        assert sentinel not in flat, f"{sentinel} survived: {flat}"
    assert run.inputs["model"] == "claude-sonnet-4-6"
    assert run.outputs["usage_metadata"]["total_tokens"] == 300


def test_llm_redactor_passes_through_already_redacted_supervisor_inputs() -> None:
    """When installed as ``Client.hide_inputs``, the llm redactor runs on
    *every* span at upload — including parent supervisor-node spans whose
    own ``process_inputs`` already produced an allowlist shape. Those
    must pass through unchanged so the safe fields survive."""

    from clinical_copilot.observability.redacting_tracer import _redact_llm_inputs

    already_redacted = {
        "request_id": "req-123",
        "patient_id_hash": "abc123",
        "user_query_length": 42,
        "sub_query_count": 2,
        "draft_count": 1,
    }

    assert _redact_llm_inputs(already_redacted) is already_redacted


def test_llm_redactor_passes_through_already_redacted_gateway_inputs() -> None:
    """Gateway LLM span's ``process_inputs`` emits a shape with
    ``message_count`` / ``message_roles`` but no raw ``messages`` list.
    hide_inputs must not mangle it."""

    from clinical_copilot.observability.redacting_tracer import _redact_llm_inputs

    gateway_redacted = {
        "model": "claude-sonnet-4-6",
        "system_prompt_length": 1200,
        "tool_def_names": ["get_observations", "get_conditions"],
        "message_count": 3,
        "message_roles": ["user", "assistant", "user"],
    }

    assert _redact_llm_inputs(gateway_redacted) is gateway_redacted


def test_llm_redactor_still_redacts_raw_wrap_anthropic_inputs() -> None:
    """Sanity: the shape detector still triggers on raw .messages.create
    kwargs — the case that was actually leaking PHI in production."""

    from clinical_copilot.observability.redacting_tracer import _redact_llm_inputs

    raw_inputs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "system": "You are the synthesizer. Patient context follows.",
        "messages": [
            {
                "role": "user",
                "content": "Worker drafts: Hashimoto's thyroiditis on levothyroxine ...",
            },
        ],
    }
    redacted = _redact_llm_inputs(raw_inputs)
    assert isinstance(redacted, dict)
    assert redacted is not raw_inputs
    flat = repr(redacted)
    for sentinel in ["Hashimoto", "levothyroxine", "Patient context"]:
        assert sentinel not in flat, f"{sentinel} survived: {flat}"
    assert redacted["model"] == "claude-sonnet-4-6"
    assert redacted["message_count"] == 1


def test_llm_redactor_passes_through_already_redacted_outputs() -> None:
    """Outputs already shaped by the gateway's ``process_outputs`` (has
    ``text_length`` / ``tool_use_count`` / ``usage_metadata`` but no raw
    ``content`` list) must survive a second pass."""

    from clinical_copilot.observability.redacting_tracer import _redact_llm_outputs

    gateway_redacted = {
        "stop_reason": "end_turn",
        "text_length": 137,
        "tool_use_names": ["get_observations"],
        "tool_use_count": 1,
        "usage_metadata": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }

    assert _redact_llm_outputs(gateway_redacted) is gateway_redacted


def test_llm_redactor_still_redacts_raw_wrap_anthropic_outputs() -> None:
    """Sanity: raw Anthropic Message dump (with ``content`` list of blocks
    and top-level ``usage_metadata``) must still be redacted."""

    from clinical_copilot.observability.redacting_tracer import _redact_llm_outputs

    raw_outputs = {
        "id": "msg_01",
        "model": "claude-sonnet-4-6",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "Patient Olivia: TSH 8.4 (Hashimoto's confirmed)."},
        ],
        "usage_metadata": {
            "input_tokens": 1234,
            "output_tokens": 567,
            "total_tokens": 1801,
        },
    }
    redacted = _redact_llm_outputs(raw_outputs)
    assert isinstance(redacted, dict)
    assert redacted is not raw_outputs
    flat = repr(redacted)
    for sentinel in ["Olivia", "TSH", "Hashimoto"]:
        assert sentinel not in flat, f"{sentinel} survived: {flat}"
    assert redacted["usage_metadata"]["total_tokens"] == 1801
    assert redacted["text_length"] == len(
        "Patient Olivia: TSH 8.4 (Hashimoto's confirmed)."
    )


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
