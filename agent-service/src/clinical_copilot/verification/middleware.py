"""Verification middleware — orchestrates the layered checks.

Order of operations:

1. **Citation existence** (:mod:`citation_check`) — every cited or
   carded ``source_id`` must resolve to a fetched record.
2. **Field-level value** (:mod:`field_check`) — when a claim asserts a
   structural field value, the value must match the record.

Either layer raising even one finding turns the response into a
``VERIFICATION_FAILED`` whole-response abstention. M2 ships the simpler
granularity rule on purpose: per-claim marking lands in PR 12, gated by
the per-lane policy.

The middleware never *modifies* the model's draft — if the draft passes,
it passes through unchanged; if it fails, the abstention replaces it.
"""

from __future__ import annotations

from clinical_copilot.orchestrator.schemas import AgentResponse, ModelDraft
from clinical_copilot.tools.records import ToolResult
from clinical_copilot.verification.abstention import Abstention, AbstentionState
from clinical_copilot.verification.citation_check import find_unresolved_citations
from clinical_copilot.verification.field_check import (
    FieldCheckError,
    FieldMismatch,
    find_field_mismatches,
)


class VerificationMiddleware:
    """Stateless verifier — the same instance handles every request.

    Constructed once at app startup; injected into the orchestrator. Has
    no I/O so it can be exercised purely from a fixture.
    """

    def verify(
        self,
        *,
        draft: ModelDraft,
        tool_results: list[ToolResult],
    ) -> AgentResponse:
        unresolved = find_unresolved_citations(
            claims=draft.prose,
            cards=draft.cards,
            tool_results=tool_results,
        )
        if unresolved:
            return AgentResponse(
                cards=[],
                prose=[],
                tool_results=tool_results,
                abstention=Abstention(
                    state=AbstentionState.VERIFICATION_FAILED,
                    reason=_format_unresolved(unresolved),
                ),
            )

        try:
            mismatches = find_field_mismatches(
                claims=draft.prose,
                tool_results=tool_results,
            )
        except FieldCheckError as exc:
            return AgentResponse(
                cards=[],
                prose=[],
                tool_results=tool_results,
                abstention=Abstention(
                    state=AbstentionState.VERIFICATION_FAILED,
                    reason=f"field check rejected the draft: {exc}",
                ),
            )

        if mismatches:
            return AgentResponse(
                cards=[],
                prose=[],
                tool_results=tool_results,
                abstention=Abstention(
                    state=AbstentionState.VERIFICATION_FAILED,
                    reason=_format_mismatches(mismatches),
                ),
            )

        return AgentResponse(
            cards=draft.cards,
            prose=draft.prose,
            tool_results=tool_results,
            abstention=None,
        )


_UNRESOLVED_PREVIEW = 3


def _format_unresolved(unresolved: list[str]) -> str:
    head = ", ".join(unresolved[:_UNRESOLVED_PREVIEW])
    suffix = (
        ""
        if len(unresolved) <= _UNRESOLVED_PREVIEW
        else f" (+{len(unresolved) - _UNRESOLVED_PREVIEW} more)"
    )
    return f"unresolved citation source_id(s): {head}{suffix}"


def _format_mismatches(mismatches: list[FieldMismatch]) -> str:
    # Mismatches carry expected/actual pairs; one-line summary is enough
    # for the abstention reason — full detail goes into the trace via
    # the LangSmith span attached by PR M4.
    return f"field-value mismatch on {len(mismatches)} claim(s)"
