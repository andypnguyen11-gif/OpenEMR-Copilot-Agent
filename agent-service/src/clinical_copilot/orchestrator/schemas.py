"""Pydantic schemas for the orchestrator's structured response.

The model emits JSON conforming to :class:`ModelDraft`. The orchestrator
adds the server-side ``tool_results`` and the optional ``abstention``,
producing :class:`AgentResponse` — that is what the UI renders.

Splitting the schema in two keeps the trust boundary visible: anything
the model wrote is in ``ModelDraft``; anything the server attests is
outside it.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinical_copilot.tools.records import ToolResult
from clinical_copilot.verification.abstention import Abstention, ClaimAbstention


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CardKind(str):
    """Allowed values for :attr:`Card.kind`.

    Kept as a plain class of constants rather than an enum because the
    set is open — PR 16's Daily Brief introduces additional card kinds
    (per-flag, per-visit) that are still cards in the same sense.
    """

    PROBLEMS = "problems"
    MEDS = "meds"
    ALLERGIES = "allergies"
    LABS = "labs"
    VISITS = "visits"
    NOTES = "notes"
    FLAGS = "flags"


class Card(_Frozen):
    """Retrieval-first surface element rendered from records, never prose.

    The card aggregates one or more ``source_id`` values from
    ``tool_results``. The verification middleware joins these the same
    way it joins ``CitedClaim.source_id`` — a card pointing at a missing
    record is as much a verification failure as prose pointing at one.
    """

    title: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_ids: list[str]


class CitedClaim(_Frozen):
    """One sentence of synthesis, with the source it leans on.

    ``source_field`` and ``expected_value`` are optional because some
    claims summarize a record (the existence is the claim) rather than
    asserting a specific field value. When both are present, the
    field-check layer asserts the record's field equals the value
    (string comparison after trim/lowercase per the field rules in PR 11).
    Setting only one is rejected at parse time — a half-specified
    assertion would silently skip field-check (``field_check.py`` short-
    circuits when either is None), so the schema refuses to accept it.
    """

    text: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_field: str | None = None
    expected_value: str | None = None

    @model_validator(mode="after")
    def _field_assertion_must_be_complete(self) -> Self:
        if (self.source_field is None) != (self.expected_value is None):
            raise ValueError(
                "source_field and expected_value must both be set or both omitted",
            )
        return self


class ModelDraft(_Frozen):
    """The JSON shape we ask the model to emit in its final turn.

    Schema-violation retry: one shot — if Pydantic rejects the model's
    output, the orchestrator re-prompts with the exact validation error
    and a single retry. A second failure becomes a
    ``VERIFICATION_FAILED`` whole-response abstention (ARCHITECTURE §7).
    """

    cards: list[Card]
    prose: list[CitedClaim]


class AgentResponse(_Frozen):
    """Final response the gateway hands back to the PHP side.

    The UI renders ``cards`` and ``prose`` only when ``abstention`` is
    None; otherwise it renders the abstention state. ``tool_results``
    are returned for traceability — the side panel uses them to build
    the "show source" hover.

    ``dropped_claims`` is the slow-lane sidecar (PR 12 / ARCHITECTURE §3
    granularity rule). Slow-lane verification removes offending items
    from ``cards`` / ``prose`` and appends one entry per drop here so
    the UI can render a redaction marker where the item used to be.
    Empty on every fast-lane response and on slow-lane responses that
    pass verification cleanly — its presence implies a partial trust
    failure, while a non-None ``abstention`` implies a total one.

    ``session_id`` is the server's canonical id for the conversation
    this response belongs to. The client echoes it on the next turn to
    continue the session; an unknown/foreign id at the next turn is
    silently replaced with a fresh one (see :class:`SessionStore`).

    The field defaults to an empty string so that intermediate
    construction sites (the verification middleware, the orchestrator's
    abstention paths) don't need to thread the canonical id all the way
    down. :meth:`Orchestrator.run` is responsible for stamping the real
    id via ``model_copy`` before the response leaves the service —
    every wire response carries a non-empty id by that contract. A
    response leaking with ``session_id=""`` is a bug.
    """

    cards: list[Card]
    prose: list[CitedClaim]
    tool_results: list[ToolResult]
    abstention: Abstention | None = None
    dropped_claims: list[ClaimAbstention] = Field(default_factory=list)
    session_id: str = Field(default="", max_length=64)
