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


def _pick_redactors(
    run_type: str | None,
    name: str | None,
) -> tuple[Callable[[Any], dict[str, Any]] | None, Callable[[Any], dict[str, Any]] | None]:
    if run_type == "chain" and name in SUPERVISOR_NODE_NAMES:
        return redact_supervisor_node_inputs, redact_supervisor_node_outputs
    if run_type == "chain":
        return _strict_drop, _strict_drop
    if run_type in {"llm", "tool"}:
        # @traceable wrappers handle these via process_inputs/process_outputs
        # and the safe fields they emit (usage_metadata, model name,
        # tool_use_names) must survive untouched so cost + tokens compute.
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
