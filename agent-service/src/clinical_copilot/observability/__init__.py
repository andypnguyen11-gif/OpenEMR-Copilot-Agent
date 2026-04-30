"""Observability surface (LangSmith tracing + PHI redaction).

Exposed at the package level so call sites import the decorator from a
stable name rather than reaching into ``tracing`` directly.
"""

from __future__ import annotations

from clinical_copilot.observability.tracing import (
    configure_tracing,
    traceable_llm_complete,
    traceable_orchestrator_run,
    traceable_tool_dispatch,
)

__all__ = [
    "configure_tracing",
    "traceable_llm_complete",
    "traceable_orchestrator_run",
    "traceable_tool_dispatch",
]
