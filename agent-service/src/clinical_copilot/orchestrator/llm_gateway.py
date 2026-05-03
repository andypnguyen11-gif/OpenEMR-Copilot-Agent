"""Thin wrapper over the Anthropic SDK.

The orchestrator depends on the :class:`LlmGateway` protocol, not on the
concrete SDK class. That serves two ends:

* **Testability.** Unit tests pass a stub gateway with canned turns so
  the tool-use loop can be exercised without a real API call.
* **Prompt caching is owned here.** Cache-control markers live in this
  module; the orchestrator does not know prompt caching exists. PRD §13
  cost budget depends on this — moving the markers is a single-file
  change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from clinical_copilot.observability import traceable_llm_complete

if TYPE_CHECKING:
    from anthropic import Anthropic


@dataclass(frozen=True, slots=True)
class ToolUse:
    """One ``tool_use`` block emitted by the model.

    ``id`` is the tool-use UUID Anthropic returns; we echo it back in
    the matching ``tool_result`` block on the next user turn so the
    SDK can pair them.
    """

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LlmTurn:
    """A normalized turn the gateway returns.

    ``text`` is the concatenation of any ``text`` blocks (in order).
    ``tool_uses`` is the list of any ``tool_use`` blocks. ``stop_reason``
    is the SDK's reason — ``end_turn`` means the model is done,
    ``tool_use`` means it expects tool results before continuing.
    """

    stop_reason: str
    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    raw_assistant_blocks: list[dict[str, Any]] = field(default_factory=list)


class LlmGateway(Protocol):
    """Minimum surface the orchestrator needs from a chat-completion API."""

    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
    ) -> LlmTurn: ...


class AnthropicLlmGateway:
    """Production gateway: real Anthropic SDK with prompt caching wired in.

    Two cache breakpoints, both ``ephemeral``: one on the system prompt
    and one on the last tool definition. Anthropic's caching applies the
    breakpoint to *everything before* the marker, so this caches the
    whole tool-defs array plus the system block — exactly the static
    portion of the prompt.
    """

    def __init__(
        self,
        *,
        client: Anthropic,
        model: str,
        max_tokens: int = 4096,
    ) -> None:
        self._client = client
        self.model = model
        self._max_tokens = max_tokens

    @traceable_llm_complete
    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
    ) -> LlmTurn:
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        tool_payload: list[dict[str, Any]] = []
        for index, tool_def in enumerate(tools):
            entry = dict(tool_def)
            if index == len(tools) - 1:
                entry["cache_control"] = {"type": "ephemeral"}
            tool_payload.append(entry)

        # The SDK accepts TypedDicts at runtime; cast through Any rather
        # than importing the SDK's narrow union types (each is a moving
        # target across SDK versions and adds zero safety here — the
        # JSON-Schema dicts are the actual contract with the wire).
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=cast(Any, system_blocks),
            tools=cast(Any, tool_payload),
            messages=cast(Any, list(messages)),
        )

        text_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        raw_blocks: list[dict[str, Any]] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
                raw_blocks.append({"type": "text", "text": getattr(block, "text", "")})
            elif block_type == "tool_use":
                tool_uses.append(
                    ToolUse(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        input=dict(getattr(block, "input", {}) or {}),
                    )
                )
                raw_blocks.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": dict(getattr(block, "input", {}) or {}),
                    }
                )

        return LlmTurn(
            stop_reason=str(getattr(response, "stop_reason", "")),
            text="".join(text_parts),
            tool_uses=tool_uses,
            raw_assistant_blocks=raw_blocks,
        )
