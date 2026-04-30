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

from langsmith import traceable

from clinical_copilot.observability.redaction import (
    configure_redaction_salt,
    redact_llm_inputs,
    redact_llm_outputs,
    redact_orchestrator_inputs,
    redact_orchestrator_outputs,
    redact_tool_dispatch_inputs,
    redact_tool_outputs,
)


def configure_tracing(*, audit_salt: str) -> None:
    """Bind the redaction salt to the runtime audit salt."""

    configure_redaction_salt(audit_salt)


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
