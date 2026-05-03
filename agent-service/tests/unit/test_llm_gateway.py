"""Unit tests for :mod:`clinical_copilot.orchestrator.llm_gateway`.

Most of the gateway's behavior (prompt-cache markers, response shaping)
is exercised end-to-end through the orchestrator tests. What needs
explicit pinning here is the **SDK exception translation** boundary
that PR 25 introduces: any Anthropic ``APIError`` subclass raised from
``client.messages.create`` must surface as :class:`LlmGatewayError` —
not the raw SDK exception — so the orchestrator catches one local
class instead of importing the SDK hierarchy.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import APIConnectionError, APITimeoutError, RateLimitError

from clinical_copilot.orchestrator.llm_gateway import (
    AnthropicLlmGateway,
    LlmGatewayError,
)

# Construct a minimal httpx.Request the SDK exception classes can carry.
# The exception constructors require it for parity with real network
# failures, but the translation logic never inspects it.
_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
_RESPONSE = httpx.Response(429, request=_REQUEST)


def _make_gateway(client: object) -> AnthropicLlmGateway:
    return AnthropicLlmGateway(client=client, model="claude-test")  # type: ignore[arg-type]


def _call(gateway: AnthropicLlmGateway) -> Any:
    return gateway.complete(system="sys", tools=[], messages=[])


def test_api_timeout_translates_to_llm_gateway_error() -> None:
    """A network-level timeout from the SDK becomes ``LlmGatewayError``
    with ``kind`` set to the SDK class name — useful in logs, opaque to
    the orchestrator's reason string."""

    client = MagicMock()
    client.messages.create.side_effect = APITimeoutError(request=_REQUEST)
    gateway = _make_gateway(client)

    with pytest.raises(LlmGatewayError) as excinfo:
        _call(gateway)
    assert excinfo.value.kind == "APITimeoutError"
    assert isinstance(excinfo.value.__cause__, APITimeoutError)


def test_api_connection_error_translates_to_llm_gateway_error() -> None:
    client = MagicMock()
    client.messages.create.side_effect = APIConnectionError(request=_REQUEST)
    gateway = _make_gateway(client)

    with pytest.raises(LlmGatewayError) as excinfo:
        _call(gateway)
    assert excinfo.value.kind == "APIConnectionError"


def test_rate_limit_error_translates_to_llm_gateway_error() -> None:
    """429 from the API surfaces as the same opaque LlmGatewayError —
    the orchestrator does not branch on rate-limit-vs-other; both are
    transient and both produce a TOOL_FAILURE abstention."""

    client = MagicMock()
    client.messages.create.side_effect = RateLimitError(
        "rate limit exceeded", response=_RESPONSE, body=None
    )
    gateway = _make_gateway(client)

    with pytest.raises(LlmGatewayError) as excinfo:
        _call(gateway)
    assert excinfo.value.kind == "RateLimitError"


def test_non_api_error_propagates_unwrapped() -> None:
    """Programming errors (TypeError from a bad SDK arg, etc.) are not
    transient — wrapping them would mask bugs. They propagate as-is so
    pytest / the FastAPI 500 handler surfaces the real cause."""

    client = MagicMock()
    client.messages.create.side_effect = TypeError("bug in caller")
    gateway = _make_gateway(client)

    with pytest.raises(TypeError, match="bug in caller"):
        _call(gateway)
