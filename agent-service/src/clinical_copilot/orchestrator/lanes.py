"""Lane primitives — per-lane configuration the orchestrator routes by.

ARCHITECTURE §2 splits the agent into two lanes that share one
implementation: a slow lane (full tool surface, larger model, deliberate
synthesis) and a fast lane (compressed prompt, flag-first tool subset,
small model, ~5s p50 budget per PRD §13). The orchestrator code path is
identical; only the configuration differs.

A :class:`LaneConfig` bundles the three things that vary per lane:

* ``llm`` — the gateway used for this lane. Each lane holds its own
  :class:`AnthropicLlmGateway` instance bound to its own model id, so an
  eval can A/B Sonnet vs Haiku by editing env vars without code changes.
* ``system_prompt`` — fast lane gets ``system_fast.md`` (compressed,
  flag-first); slow lane gets ``system_slow.md`` (full hard-rules
  preamble).
* ``tool_names`` — the subset of registry tools the lane is allowed to
  call. ``None`` means "every tool the registry knows about." Fast lane
  pins this to the four tools that match its latency budget.

The orchestrator's per-turn flow looks the lane up once at the top of
:meth:`Orchestrator.run` and pulls everything from one
:class:`LaneConfig` after that — no further branching on lane in the
loop body. That keeps the contract crisp: "same code path, different
configs."
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clinical_copilot.orchestrator.llm_gateway import LlmGateway


class Lane(StrEnum):
    """Which lane configuration this request runs against.

    String-backed because the value crosses the JSON wire boundary —
    PHP gateway sends ``"slow"`` / ``"fast"``, FastAPI / Pydantic parses
    it, the orchestrator routes by it. Backed enum (not unit enum) is
    the right choice when a value is serialized; see CLAUDE.md "Type
    System".
    """

    SLOW = "slow"
    FAST = "fast"


@dataclass(frozen=True, slots=True)
class LaneConfig:
    """One lane's worth of configuration.

    ``tool_names=None`` is the "all tools" sentinel, used by the slow
    lane (which gets the registry's full tool set). Fast lane pins this
    to a literal subset; the orchestrator filters
    :meth:`ToolRegistry.anthropic_schemas` and rejects any model
    ``tool_use`` for a name outside the set. The check is enforced at
    dispatch time as defense-in-depth — the prompt already advertises
    only the subset, but a malformed model output mustn't reach the
    tool layer.
    """

    llm: LlmGateway
    system_prompt: str
    tool_names: frozenset[str] | None = None
