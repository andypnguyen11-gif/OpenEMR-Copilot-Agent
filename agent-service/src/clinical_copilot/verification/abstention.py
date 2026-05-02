"""Four-state abstention taxonomy + per-lane granularity markers.

PRD §5 / ARCHITECTURE §3. Every place the agent declines to answer maps
to exactly one of these states. The state is the contract — the prose
attached to it is UX copy, not part of the trust surface.

The granularity rule is per-lane (PR 12):

* **Fast lane** — any verification failure collapses the whole response
  to a single :class:`Abstention`. Latency budget rules; the side panel
  cannot afford the UI cost of partial-render markers.
* **Slow lane** — verification failures drop only the offending claim
  or card; surviving items render unchanged. Each drop becomes one
  :class:`ClaimAbstention` in the response's ``dropped_claims`` sidecar
  list, which the UI uses to show a redaction marker in place of the
  removed item.

Programming/model errors (unknown field name, missing categorical vocab)
collapse to whole-response on either lane: a model that invented a
field name is suspect across all its claims, not just the one we
tripped on first.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AbstentionState(StrEnum):
    """Why the agent stopped short of a confident answer.

    String-backed because the value is serialized to the wire (response
    body) and into LangSmith trace metadata. Adding a new state is a
    backwards-compatible change; renaming an existing one is not — the
    UI's per-state copy table is keyed on these strings.
    """

    NO_DATA = "NO_DATA"
    """The chart legitimately doesn't contain what the user asked for."""

    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    """The model produced a draft, but at least one cited claim either
    points at a source the agent never fetched or contradicts the field
    value of a source the agent did fetch."""

    TOOL_FAILURE = "TOOL_FAILURE"
    """A tool the orchestrator needed to call raised a non-authorization
    error (timeout, FHIR 5xx, schema-mismatch). Distinct from NO_DATA so
    the UI can offer a retry action."""

    UNAUTHORIZED = "UNAUTHORIZED"
    """The session is not authorized to access the requested resource —
    raised when a tool's RBAC check fires (audit row already written by
    the tool layer per ARCHITECTURE §3 table)."""


class Abstention(BaseModel):
    """Response-level abstention. Replaces ``cards`` and ``prose`` when
    the whole response cannot be trusted.

    ``reason`` is a server-side diagnostic that is safe to render as a
    short user-facing line — it does not contain PHI or vendor message
    text. The UI maps ``state`` to its localized copy regardless of the
    server-side reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: AbstentionState
    reason: str


class ClaimAbstention(BaseModel):
    """Per-claim (or per-card) marker emitted by the slow lane.

    Slow-lane verification drops the offending claim/card from the
    rendered response and appends one of these to
    :attr:`AgentResponse.dropped_claims`. The UI uses the entries to
    render a redaction marker where the dropped item used to sit; the
    unverified text never crosses the wire.

    ``source_id`` identifies the dropped item's underlying record.
    ``source_field`` is set for field-mismatch drops (so the UI can show
    "claimed value did not match" against a specific field) and ``None``
    for citation-existence drops, which include cards (a card is a
    multi-source aggregate, not a single field assertion).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_field: str | None = None
    state: AbstentionState
    reason: str
