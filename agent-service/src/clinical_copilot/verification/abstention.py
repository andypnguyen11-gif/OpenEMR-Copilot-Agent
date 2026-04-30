"""Four-state abstention taxonomy.

PRD §5 / ARCHITECTURE §3. Every place the agent declines to answer maps
to exactly one of these states. The state is the contract — the prose
attached to it is UX copy, not part of the trust surface.

For M2 the granularity rule is *whole-response abstain* on any
verification failure (the simpler model). PR 12 splits per-lane: fast
lane keeps whole-response, slow lane goes per-claim.
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
    """Wrapper the orchestrator attaches to an :class:`AgentResponse`.

    ``reason`` is a server-side diagnostic that is safe to render as a
    short user-facing line — it does not contain PHI or vendor message
    text. The UI maps ``state`` to its localized copy regardless of the
    server-side reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: AbstentionState
    reason: str
