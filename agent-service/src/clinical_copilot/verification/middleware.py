"""Verification middleware — orchestrates the layered checks.

Order of operations:

1. **Citation existence** (:mod:`citation_check`) — every cited or
   carded ``source_id`` must resolve to a fetched record.
2. **Field-level value** (:mod:`field_check`) — when a claim asserts a
   structural field value, the value must match the record.

The granularity of a verification failure is per-lane (PR 12,
ARCHITECTURE §3):

* **Fast lane** — any failure collapses the whole response to a single
  ``VERIFICATION_FAILED`` :class:`Abstention`. The side panel cannot
  afford the UI cost of partial-render markers within its latency
  budget.
* **Slow lane** — failures drop only the offending claim or card,
  appending one :class:`ClaimAbstention` to ``dropped_claims`` per
  drop. Surviving items render unchanged. If every claim/card was
  dropped, we escalate back to a response-level abstention — there is
  nothing left to render.

A :class:`FieldCheckError` (model named a field that doesn't exist on
the cited record, or a CATEGORICAL field with no wired vocab) collapses
the whole response on either lane: a model that invented a field name
is suspect across all its claims, not just the one we tripped on first.

The middleware never *modifies* the model's draft beyond filtering —
surviving claims and cards pass through byte-identical.
"""

from __future__ import annotations

from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.schemas import AgentResponse, Card, CitedClaim, ModelDraft
from clinical_copilot.tools.records import ToolResult
from clinical_copilot.verification.abstention import (
    Abstention,
    AbstentionState,
    ClaimAbstention,
)
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
        lane: Lane = Lane.SLOW,
    ) -> AgentResponse:
        unresolved = find_unresolved_citations(
            claims=draft.prose,
            cards=draft.cards,
            tool_results=tool_results,
        )

        try:
            mismatches = find_field_mismatches(
                claims=draft.prose,
                tool_results=tool_results,
            )
        except FieldCheckError as exc:
            # Programming/model error — collapse on either lane. See
            # module docstring for the rationale.
            return _whole_response_abstain(
                tool_results=tool_results,
                reason=f"field check rejected the draft: {exc}",
            )

        if not unresolved and not mismatches:
            return AgentResponse(
                cards=draft.cards,
                prose=draft.prose,
                tool_results=tool_results,
                abstention=None,
            )

        if lane is Lane.FAST:
            return _whole_response_abstain(
                tool_results=tool_results,
                reason=_format_combined(unresolved, mismatches),
            )

        return _slow_lane_partial(
            draft=draft,
            tool_results=tool_results,
            unresolved=unresolved,
            mismatches=mismatches,
        )


def _whole_response_abstain(
    *,
    tool_results: list[ToolResult],
    reason: str,
) -> AgentResponse:
    return AgentResponse(
        cards=[],
        prose=[],
        tool_results=tool_results,
        abstention=Abstention(
            state=AbstentionState.VERIFICATION_FAILED,
            reason=reason,
        ),
    )


def _slow_lane_partial(
    *,
    draft: ModelDraft,
    tool_results: list[ToolResult],
    unresolved: list[str],
    mismatches: list[FieldMismatch],
) -> AgentResponse:
    """Drop offending claims/cards and emit one ClaimAbstention per drop.

    If every item drops, escalate to a response-level abstention — an
    empty body with no abstention would render as "the agent had
    nothing to say," which is wrong here: the agent had things to say,
    and we rejected all of them.
    """

    unresolved_set = set(unresolved)
    # Index mismatches by (source_id, source_field, expected) so two
    # claims pointing at the same field on the same record but
    # asserting different values are disambiguated. Without
    # ``expected`` in the key, a passing claim sharing field+source
    # with a failing one would be dropped alongside the offender.
    mismatch_by_key: dict[tuple[str, str, str], FieldMismatch] = {
        (m.source_id, m.source_field, m.expected): m for m in mismatches
    }

    surviving_prose: list[CitedClaim] = []
    dropped: list[ClaimAbstention] = []

    for claim in draft.prose:
        if claim.source_id in unresolved_set:
            dropped.append(
                ClaimAbstention(
                    source_id=claim.source_id,
                    source_field=claim.source_field,
                    state=AbstentionState.VERIFICATION_FAILED,
                    reason=f"unresolved citation source_id {claim.source_id!r}",
                ),
            )
            continue
        if claim.source_field is not None and claim.expected_value is not None:
            mismatch = mismatch_by_key.get(
                (claim.source_id, claim.source_field, claim.expected_value),
            )
            if mismatch is not None:
                dropped.append(
                    ClaimAbstention(
                        source_id=claim.source_id,
                        source_field=claim.source_field,
                        state=AbstentionState.VERIFICATION_FAILED,
                        reason=(
                            f"field-value mismatch on {mismatch.source_field!r}: "
                            f"claimed {mismatch.expected!r}, record has "
                            f"{mismatch.actual!r}"
                        ),
                    ),
                )
                continue
        surviving_prose.append(claim)

    surviving_cards: list[Card] = []
    for card in draft.cards:
        bad_sources = [sid for sid in card.source_ids if sid in unresolved_set]
        if not bad_sources:
            surviving_cards.append(card)
            continue
        # Card granularity is per-card, not per-source-id within a
        # card: a problems-card claims to project the patient's active
        # problems, and rendering it with hidden bad entries would let
        # the model's fabricated source quietly steer the trim. Drop
        # the whole card and surface one entry per fabricated source so
        # the trail is auditable.
        for sid in bad_sources:
            dropped.append(
                ClaimAbstention(
                    source_id=sid,
                    source_field=None,
                    state=AbstentionState.VERIFICATION_FAILED,
                    reason=(f"card {card.title!r} cited unresolved source_id {sid!r}"),
                ),
            )

    if not surviving_prose and not surviving_cards:
        return AgentResponse(
            cards=[],
            prose=[],
            tool_results=tool_results,
            abstention=Abstention(
                state=AbstentionState.VERIFICATION_FAILED,
                reason=_format_combined(unresolved, mismatches),
            ),
            dropped_claims=dropped,
        )

    return AgentResponse(
        cards=surviving_cards,
        prose=surviving_prose,
        tool_results=tool_results,
        abstention=None,
        dropped_claims=dropped,
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


def _format_combined(
    unresolved: list[str],
    mismatches: list[FieldMismatch],
) -> str:
    """One reason string covering whichever layers fired. Used by both
    the fast-lane whole-response path and the slow-lane "everything
    dropped" escalation so the wire reason stays consistent across
    lanes — same trust verdict, same string."""

    parts: list[str] = []
    if unresolved:
        parts.append(_format_unresolved(unresolved))
    if mismatches:
        parts.append(_format_mismatches(mismatches))
    return "; ".join(parts)
