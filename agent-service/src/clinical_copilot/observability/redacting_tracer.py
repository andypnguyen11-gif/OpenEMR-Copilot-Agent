"""Redacting :class:`LangChainTracer` subclass.

LangGraph's auto-instrumentation routes node spans through the LangChain
callback system, which uses :class:`LangChainTracer` to upload them. Our
``@traceable`` decorators (orchestrator, llm, tool, supervisor node) take
a separate path through :mod:`langsmith.run_helpers` and apply their own
``process_inputs`` / ``process_outputs`` redactors before the run reaches
LangSmith.

That split means PHI-bearing fields on LangGraph's auto-spans (raw
``TurnState`` as inputs, raw return dicts as outputs) bypass the
``@traceable`` redactors entirely and land in LangSmith verbatim. The
``callbacks=[]`` mitigation we tried doesn't help — the LangSmith tracer
is attached at the env-var level, not via the per-invocation callbacks
list.

This subclass closes that hole by intercepting every run on its way to
the LangSmith client and applying the matching allowlist redactor based
on ``run.run_type`` and ``run.name``. It must be installed as the
``tracing_v2_callback_var`` at app startup so the default tracer
construction in :func:`langchain_core.tracers.context._get_trace_callbacks`
returns our subclass.

Order of redaction:
  * Supervisor node (``run_type='chain'`` and a known node name) → the
    existing ``redact_supervisor_node_inputs/outputs`` pair.
  * Any other ``run_type='chain'`` (LangGraph internals like
    ``ChannelWrite``, ``_planner_router``, the top-level ``LangGraph``
    span) → strict drop. These spans carry the same state dict so they
    leak the same PHI; we don't have a separate semantic for them.
  * ``run_type='llm'`` and ``run_type='tool'`` → no-op. Our
    ``traceable_llm_complete`` / ``traceable_tool_dispatch`` decorators
    already redact via ``process_inputs/process_outputs`` and we don't
    want to double-redact (the safe surface they emit — model name,
    usage_metadata, tool_use_names — must survive).
  * Anything else → strict drop, defense in depth.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from langchain_core.tracers.langchain import LangChainTracer
from langsmith.schemas import Run

from clinical_copilot.observability.redaction import (
    redact_supervisor_node_inputs,
    redact_supervisor_node_outputs,
)


# Mirrors the constants in orchestrator/supervisor_langgraph.py. Duplicated
# here to avoid a circular import (the tracer is configured in the
# composition root, which imports the orchestrator package).
SUPERVISOR_NODE_NAMES: Final[frozenset[str]] = frozenset({
    "planner",
    "v1_single",
    "intake_extractor",
    "evidence_retriever",
    "synthesizer",
    "critic",
    "verification",
})


def _strict_drop(_payload: object) -> dict[str, Any]:
    """Default redactor for unrecognized chain spans.

    LangGraph emits spans for internal plumbing (ChannelWrite, routers,
    the top-level graph itself) whose payload is the full ``TurnState``.
    We have no allowlist for these because they're not application code,
    so the safe move is to drop everything and surface only a marker
    that an event happened. Length / shape introspection on an opaque
    object is itself a PHI risk if a free-text string sneaks in.
    """

    return {"redacted": True}


def _redact_llm_inputs(inputs: object) -> dict[str, Any]:
    """Redactor for ``llm`` run inputs from any source.

    Covers both ``@traceable_llm_complete`` (kwargs from
    ``LlmGateway.complete``: ``self``, ``system``, ``tools``,
    ``messages``) and ``langsmith.wrappers.wrap_anthropic``
    (kwargs from ``Anthropic.messages.create``: ``model``,
    ``max_tokens``, ``system``, ``tools``, ``messages``). Both shapes
    carry the entire conversation including the system prompt, all user
    turns, and any prior assistant tool-result blocks — every one of
    which can contain PHI. The allowlist surfaces lengths and counts
    only.
    """

    redacted: dict[str, Any] = {}
    if not isinstance(inputs, dict):
        return redacted

    # Model name: wrap_anthropic exposes "model" at the top level;
    # @traceable on the gateway exposes it via the bound ``self``.
    model = inputs.get("model")
    if not isinstance(model, str):
        gateway = inputs.get("self")
        model = getattr(gateway, "model", None)
    if isinstance(model, str):
        redacted["model"] = model

    max_tokens = inputs.get("max_tokens")
    if isinstance(max_tokens, int):
        redacted["max_tokens"] = max_tokens

    system = inputs.get("system")
    if isinstance(system, str):
        redacted["system_prompt_length"] = len(system)
    elif isinstance(system, list):
        # wrap_anthropic preserves the list-of-blocks shape we use for
        # prompt caching. Sum text-block lengths for a single number.
        redacted["system_prompt_length"] = sum(
            len(b.get("text", "")) for b in system if isinstance(b, dict)
        )

    tools = inputs.get("tools") or []
    if isinstance(tools, list):
        redacted["tool_def_names"] = [
            t["name"]
            for t in tools
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]

    messages = inputs.get("messages") or []
    if isinstance(messages, list):
        redacted["message_count"] = len(messages)
        redacted["message_roles"] = [
            m["role"]
            for m in messages
            if isinstance(m, dict) and isinstance(m.get("role"), str)
        ]

    return redacted


def _redact_llm_outputs(outputs: object) -> dict[str, Any]:
    """Redactor for ``llm`` run outputs from any source.

    Covers both the ``LlmTurn`` dataclass our gateway returns (with
    ``text``, ``tool_uses``, ``stop_reason``, ``input_tokens``,
    ``output_tokens``) and the raw ``anthropic.types.Message`` that
    ``wrap_anthropic`` records (with ``content`` blocks and ``usage``).
    The model's free-form text is the highest-volume PHI risk in the
    pipeline — surface its length only. Token usage is preserved in the
    LangChain-standard ``usage_metadata`` shape so the LangSmith Tokens
    and Cost columns compute.
    """

    redacted: dict[str, Any] = {
        "stop_reason": None,
        "text_length": 0,
        "tool_use_names": [],
        "tool_use_count": 0,
        "usage_metadata": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }
    if outputs is None:
        return redacted

    # 1) LlmTurn shape (gateway path).
    if hasattr(outputs, "text") and hasattr(outputs, "tool_uses"):
        text = getattr(outputs, "text", "") or ""
        tool_uses = getattr(outputs, "tool_uses", None) or []
        in_t = int(getattr(outputs, "input_tokens", 0) or 0)
        out_t = int(getattr(outputs, "output_tokens", 0) or 0)
        redacted.update(
            {
                "stop_reason": getattr(outputs, "stop_reason", None),
                "text_length": len(text),
                "tool_use_names": [
                    getattr(u, "name", None)
                    for u in tool_uses
                    if isinstance(getattr(u, "name", None), str)
                ],
                "tool_use_count": len(tool_uses),
                "usage_metadata": {
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "total_tokens": in_t + out_t,
                },
            }
        )
        return redacted

    # 2) wrap_anthropic shape — outputs is a dict that wraps the
    # anthropic Message. Walk content blocks for text length and tool_use
    # names; pull usage off either ``usage`` or ``usage_metadata``.
    if isinstance(outputs, dict):
        # _process_chat_completion in langsmith.wrappers._anthropic
        # surfaces the message body under various keys; check both.
        msg = outputs.get("output") or outputs
        content = []
        if isinstance(msg, dict):
            content = msg.get("content") or []
            redacted["stop_reason"] = msg.get("stop_reason")
        elif hasattr(msg, "content"):
            content = getattr(msg, "content", []) or []
            redacted["stop_reason"] = getattr(msg, "stop_reason", None)

        text_len = 0
        tool_names: list[str] = []
        for block in content:
            block_type = (
                block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            )
            if block_type == "text":
                text = (
                    block.get("text", "")
                    if isinstance(block, dict)
                    else getattr(block, "text", "")
                )
                text_len += len(text or "")
            elif block_type == "tool_use":
                name = (
                    block.get("name")
                    if isinstance(block, dict)
                    else getattr(block, "name", None)
                )
                if isinstance(name, str):
                    tool_names.append(name)
        redacted["text_length"] = text_len
        redacted["tool_use_names"] = tool_names
        redacted["tool_use_count"] = len(tool_names)

        # Token usage: prefer the LangChain-standard shape if already set
        # (wrap_anthropic does this), else parse from the raw ``usage``.
        usage_md = outputs.get("usage_metadata") if isinstance(outputs, dict) else None
        if isinstance(usage_md, dict):
            redacted["usage_metadata"] = {
                "input_tokens": int(usage_md.get("input_tokens", 0) or 0),
                "output_tokens": int(usage_md.get("output_tokens", 0) or 0),
                "total_tokens": int(usage_md.get("total_tokens", 0) or 0),
            }
        else:
            usage = outputs.get("usage") if isinstance(outputs, dict) else None
            if isinstance(usage, dict):
                in_t = int(usage.get("input_tokens", 0) or 0)
                out_t = int(usage.get("output_tokens", 0) or 0)
                redacted["usage_metadata"] = {
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "total_tokens": in_t + out_t,
                }

    return redacted


def _pick_redactors(
    run_type: str | None,
    name: str | None,
) -> tuple[Callable[[Any], dict[str, Any]] | None, Callable[[Any], dict[str, Any]] | None]:
    if run_type == "chain" and name in SUPERVISOR_NODE_NAMES:
        return redact_supervisor_node_inputs, redact_supervisor_node_outputs
    if run_type == "chain":
        return _strict_drop, _strict_drop
    if run_type == "llm":
        # Catches wrap_anthropic spans (raw .messages.create from planner /
        # critic / synthesizer / v1_single) and any @traceable_llm_complete
        # span that isn't already self-redacting. The redactor preserves
        # usage_metadata + model name so cost + tokens compute.
        return _redact_llm_inputs, _redact_llm_outputs
    if run_type == "tool":
        # @traceable_tool_dispatch already applies process_inputs/outputs
        # via the decorator — its output is the safe surface (tool name,
        # record counts, hashed patient id) and re-redacting would drop
        # those signals.
        return None, None
    # Unknown run_type. Defense in depth — drop.
    return _strict_drop, _strict_drop


class RedactingLangChainTracer(LangChainTracer):
    """LangChainTracer that redacts every run before it leaves the process."""

    def _persist_run_single(self, run: Run) -> None:
        self._apply_redaction(run)
        super()._persist_run_single(run)

    def _update_run_single(self, run: Run) -> None:
        self._apply_redaction(run)
        super()._update_run_single(run)

    def _apply_redaction(self, run: Run) -> None:
        in_redactor, out_redactor = _pick_redactors(run.run_type, run.name)
        if in_redactor is not None and run.inputs:
            try:
                run.inputs = in_redactor(run.inputs)
            except Exception:
                # If the redactor crashes on an unexpected shape, fail
                # closed — drop everything rather than risk uploading PHI.
                run.inputs = _strict_drop(None)
        if out_redactor is not None and run.outputs:
            try:
                run.outputs = out_redactor(run.outputs)
            except Exception:
                run.outputs = _strict_drop(None)


def install_redacting_tracer(*, project_name: str | None = None) -> RedactingLangChainTracer:
    """Set the redacting tracer as the default for this process.

    LangChain's ``_get_trace_callbacks`` reads
    ``tracing_v2_callback_var.get() or LangChainTracer(...)`` — setting
    the context var pins our subclass as the default for every callback
    invocation in this process. Returns the installed tracer so the
    caller can hold a reference (the var holds a weak-ish ContextVar;
    keeping a strong reference avoids surprise GC in long-running
    processes).
    """

    from langchain_core.tracers.context import tracing_v2_callback_var

    tracer = RedactingLangChainTracer(project_name=project_name)
    tracing_v2_callback_var.set(tracer)
    return tracer
