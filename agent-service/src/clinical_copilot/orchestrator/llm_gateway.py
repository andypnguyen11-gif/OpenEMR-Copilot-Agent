"""Thin wrapper over the Anthropic SDK.

The orchestrator depends on the :class:`LlmGateway` protocol, not on the
concrete SDK class. That serves three ends:

* **Testability.** Unit tests pass a stub gateway with canned turns so
  the tool-use loop can be exercised without a real API call.
* **Prompt caching is owned here.** Cache-control markers live in this
  module; the orchestrator does not know prompt caching exists. PRD §13
  cost budget depends on this — moving the markers is a single-file
  change.
* **SDK exception isolation.** Transient API failures (timeouts, rate
  limits, 5xx) are translated to :class:`LlmGatewayError` here, so
  ``agent.py`` catches one local exception class instead of importing
  the Anthropic SDK's hierarchy. Programming-time errors (bad SDK
  arguments, ``AnthropicError`` subclasses that aren't ``APIError``)
  are left to propagate as bugs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from anthropic import APIError

from clinical_copilot.observability import traceable_llm_complete

if TYPE_CHECKING:
    from anthropic import Anthropic


class LlmGatewayError(Exception):
    """Raised when an :class:`LlmGateway.complete` call fails transiently.

    Wraps Anthropic ``APIError`` subclasses (``APITimeoutError``,
    ``APIConnectionError``, ``RateLimitError``, ``APIStatusError``).
    The orchestrator catches this and emits a ``TOOL_FAILURE``
    abstention; callers should never see the wrapped SDK exception
    message because it can carry internal request-id and URL details.
    """

    def __init__(self, kind: str, *, cause: BaseException | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        if cause is not None:
            self.__cause__ = cause


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

    ``input_tokens`` / ``output_tokens`` are the SDK's per-turn usage
    figures (``response.usage``). They default to ``0`` so stub gateways
    in tests don't have to populate them; production reads them off
    every Anthropic call so the orchestrator can sum the loop into one
    :class:`UsageTotals` for the trace row.
    """

    stop_reason: str
    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    raw_assistant_blocks: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


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
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=cast(Any, system_blocks),
                tools=cast(Any, tool_payload),
                messages=cast(Any, list(messages)),
            )
        except APIError as exc:
            # Translate the SDK's transient errors to a local class so
            # the orchestrator doesn't have to import anthropic. ``kind``
            # is the SDK class name — useful in logs, safe to surface
            # because it carries no PHI or request-specific detail.
            raise LlmGatewayError(type(exc).__name__, cause=exc) from exc

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

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        return LlmTurn(
            stop_reason=str(getattr(response, "stop_reason", "")),
            text="".join(text_parts),
            tool_uses=tool_uses,
            raw_assistant_blocks=raw_blocks,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
