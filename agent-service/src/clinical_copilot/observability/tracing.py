"""LangSmith ``@traceable`` wrappers wired through the redaction layer.

Each call site (orchestrator run, LLM completion, tool dispatch) is
wrapped with the matching pair of input + output redactors from
:mod:`.redaction`. This module is the only place that link is made, so
a reviewer can verify on one screen that no traced surface escapes
redaction.

``@traceable`` is a no-op when ``LANGSMITH_TRACING`` is not set, so
wrapping unconditionally costs nothing in dev/test but lights up
automatically in any environment that exports the env var.

Salt configuration is delegated to :func:`configure_tracing`, called
once from the composition root with the same value as the audit log's
salt. That match is what lets investigators join a LangSmith trace's
``patient_id_hash`` to its audit-log row.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from langsmith import traceable

from clinical_copilot.observability.redaction import (
    configure_redaction_salt,
    redact_llm_inputs,
    redact_llm_outputs,
    redact_orchestrator_inputs,
    redact_orchestrator_outputs,
    redact_supervisor_node_inputs,
    redact_supervisor_node_outputs,
    redact_tool_dispatch_inputs,
    redact_tool_outputs,
)

# Hold a strong reference to the installed tracer so a subsequent GC
# pass can't drop it from the ContextVar slot. ``configure_tracing`` is
# called once at startup; this slot is the canonical place we own that
# reference for the lifetime of the process.
_INSTALLED_TRACER: Any = None


def configure_tracing(*, audit_salt: str) -> None:
    """Bind the redaction salt to the runtime audit salt and install the
    redacting LangChain tracer.

    The tracer install only runs when LangSmith tracing is enabled in
    the env (``LANGSMITH_TRACING`` / ``LANGCHAIN_TRACING_V2``). Test and
    dev runs without those env vars stay fast — no tracer construction,
    no extra context-var manipulation.
    """

    global _INSTALLED_TRACER

    configure_redaction_salt(audit_salt)

    if os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2"):
        # Imported lazily to keep this module import-clean in environments
        # that don't have langchain_core installed (it's a transitive dep
        # of langgraph, but we don't want to assume).
        from clinical_copilot.observability.redacting_tracer import (
            install_redacting_tracer,
        )

        project = os.environ.get("LANGSMITH_PROJECT") or os.environ.get(
            "LANGCHAIN_PROJECT"
        )
        _INSTALLED_TRACER = install_redacting_tracer(project_name=project)


traceable_orchestrator_run = traceable(
    run_type="chain",
    name="orchestrator.run",
    process_inputs=redact_orchestrator_inputs,
    process_outputs=redact_orchestrator_outputs,
)

traceable_llm_complete = traceable(
    run_type="llm",
    name="llm.complete",
    process_inputs=redact_llm_inputs,
    process_outputs=redact_llm_outputs,
)

traceable_tool_dispatch = traceable(
    run_type="tool",
    name="tool.dispatch",
    process_inputs=redact_tool_dispatch_inputs,
    process_outputs=redact_tool_outputs,
)


def traceable_supervisor_node(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory for LangGraph supervisor nodes.

    Each node body receives the full ``TurnState`` and returns a partial
    state dict — both carry PHI (free-text query, drafts, synthesized
    text) that LangGraph's auto-instrumentation would otherwise emit to
    LangSmith verbatim. Wrapping the node body with this decorator runs
    the allowlist redactor pair from :mod:`.redaction` *before* the
    payload reaches the LangSmith client, mirroring the existing
    orchestrator/llm/tool wrappers above.
    """

    return traceable(
        run_type="chain",
        name=name,
        process_inputs=redact_supervisor_node_inputs,
        process_outputs=redact_supervisor_node_outputs,
    )
