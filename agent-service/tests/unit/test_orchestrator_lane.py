"""Lane-routing contract for the orchestrator.

PR 10 splits one orchestrator into two lane configurations (slow + fast).
This module pins the contract that ``Lane`` selects the right
``LaneConfig`` (system prompt, gateway, tool subset) without leaking
state across lanes:

* Slow lane request → slow gateway, slow prompt, full tool set
* Fast lane request → fast gateway, fast prompt, four-tool subset
* Fast lane refuses an out-of-subset ``tool_use`` from the model with a
  ``TOOL_FAILURE`` abstention (defense-in-depth — the prompt only
  advertises the subset, but a malformed model output mustn't reach the
  tool layer)
* Asking for an unconfigured lane raises ``UnknownLaneError`` so the
  route can surface a 400 instead of silently routing to slow

Reuses the scripted-gateway pattern from :mod:`test_orchestrator_slow`
so each lane gets a deterministic recording of the calls the
orchestrator made.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

import pytest

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.agent import Orchestrator, UnknownLaneError
from clinical_copilot.orchestrator.lanes import Lane, LaneConfig
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.orchestrator.sessions import SessionStore
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.abstention import AbstentionState
from clinical_copilot.verification.middleware import VerificationMiddleware

_FAST_LANE_TOOLS = frozenset({"get_flags", "get_problems", "get_meds", "get_visits", "get_labs"})


class _RecordingAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ScriptedGateway:
    """Pop the next pre-recorded turn off the queue per ``complete``
    call. ``calls`` records what the orchestrator handed to the gateway
    so the test can assert on prompt / tool defs / messages.
    """

    def __init__(self, turns: Sequence[LlmTurn]) -> None:
        self._turns: deque[LlmTurn] = deque(turns)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
    ) -> LlmTurn:
        self.calls.append({"system": system, "tools": list(tools), "messages": list(messages)})
        if not self._turns:
            raise AssertionError("scripted gateway exhausted")
        return self._turns.popleft()


@pytest.fixture
def claims() -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id="101",
        scopes=[
            "system/Condition.read",
            "system/MedicationRequest.read",
            "system/AllergyIntolerance.read",
            "system/Observation.read",
            "system/Encounter.read",
            "system/DocumentReference.read",
        ],
        nonce="n",
        jti="jti-1",
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.from_fixture(
        store=FixtureStore.from_file(),
        audit=_RecordingAudit(),
        audit_salt="salt",
    )


@pytest.fixture
def verifier() -> VerificationMiddleware:
    return VerificationMiddleware()


@pytest.fixture
def sessions() -> SessionStore:
    return SessionStore()


def _final_text_turn(json_payload: str) -> LlmTurn:
    return LlmTurn(
        stop_reason="end_turn",
        text=json_payload,
        tool_uses=[],
        raw_assistant_blocks=[{"type": "text", "text": json_payload}],
    )


def _tool_use_turn(*tool_uses: ToolUse) -> LlmTurn:
    blocks = [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses
    ]
    return LlmTurn(
        stop_reason="tool_use",
        text="",
        tool_uses=list(tool_uses),
        raw_assistant_blocks=blocks,
    )


def _two_lane_orch(
    *,
    slow_gateway: _ScriptedGateway,
    fast_gateway: _ScriptedGateway,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> Orchestrator:
    return Orchestrator(
        lanes={
            Lane.SLOW: LaneConfig(
                llm=slow_gateway,
                system_prompt="SLOW PROMPT",
                tool_names=None,
            ),
            Lane.FAST: LaneConfig(
                llm=fast_gateway,
                system_prompt="FAST PROMPT",
                tool_names=_FAST_LANE_TOOLS,
            ),
        },
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )


def test_slow_lane_routes_to_slow_gateway_with_full_tool_set(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """Slow-lane request hits the slow gateway with the full tool set
    and the slow prompt. Fast gateway sees no traffic.
    """

    final_json = (
        '{"cards":[{"title":"Active problems","kind":"problems",'
        '"source_ids":["Condition/p101-cond-1"]}],'
        '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
        '"source_id":"Condition/p101-cond-1"}]}'
    )
    slow_gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(final_json),
        ]
    )
    fast_gateway = _ScriptedGateway([])
    orch = _two_lane_orch(
        slow_gateway=slow_gateway,
        fast_gateway=fast_gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )

    response = orch.run(
        query="problems?",
        claims=claims,
        request_id="r-slow",
        lane=Lane.SLOW,
    )

    assert response.abstention is None
    # Slow gateway saw both turns; fast gateway saw nothing.
    assert len(slow_gateway.calls) == 2
    assert len(fast_gateway.calls) == 0
    # Slow prompt + full tool set (registry has 7 fixture tools).
    assert "SLOW PROMPT" in slow_gateway.calls[0]["system"]
    slow_tool_names = {tool["name"] for tool in slow_gateway.calls[0]["tools"]}
    assert slow_tool_names == set(registry.names())


def test_fast_lane_routes_to_fast_gateway_with_subset_tool_set(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """Fast-lane request hits the fast gateway with the four-tool
    subset and the fast prompt. Slow gateway sees no traffic.
    """

    final_json = (
        '{"cards":[{"title":"Active problems","kind":"problems",'
        '"source_ids":["Condition/p101-cond-1"]}],'
        '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
        '"source_id":"Condition/p101-cond-1"}]}'
    )
    fast_gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(final_json),
        ]
    )
    slow_gateway = _ScriptedGateway([])
    orch = _two_lane_orch(
        slow_gateway=slow_gateway,
        fast_gateway=fast_gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )

    response = orch.run(
        query="problems?",
        claims=claims,
        request_id="r-fast",
        lane=Lane.FAST,
    )

    assert response.abstention is None
    assert len(fast_gateway.calls) == 2
    assert len(slow_gateway.calls) == 0
    # Fast prompt + only the four allowed tools, regardless of registry size.
    assert "FAST PROMPT" in fast_gateway.calls[0]["system"]
    fast_tool_names = {tool["name"] for tool in fast_gateway.calls[0]["tools"]}
    assert fast_tool_names == _FAST_LANE_TOOLS


def test_fast_lane_refuses_out_of_subset_tool_call(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """If the fast-lane model emits a ``tool_use`` for a tool outside
    the lane's allowed set, the orchestrator short-circuits to
    ``TOOL_FAILURE`` rather than dispatching it. The prompt already
    advertises only the subset; this is defense-in-depth.
    """

    fast_gateway = _ScriptedGateway(
        [
            # ``get_allergies`` is registered but not in the fast subset.
            _tool_use_turn(ToolUse(id="tu-1", name="get_allergies", input={"patient_id": "101"})),
        ]
    )
    slow_gateway = _ScriptedGateway([])
    orch = _two_lane_orch(
        slow_gateway=slow_gateway,
        fast_gateway=fast_gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )

    response = orch.run(
        query="any allergies?",
        claims=claims,
        request_id="r-fast-out-of-subset",
        lane=Lane.FAST,
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.TOOL_FAILURE
    assert "not available on this lane" in response.abstention.reason
    # Only the one (rejected) tool turn was sent — no second LLM call.
    assert len(fast_gateway.calls) == 1


def test_unknown_lane_raises(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """An orchestrator wired with only the slow lane should raise
    ``UnknownLaneError`` when asked for the fast lane — the route turns
    that into a 400 rather than silently routing to slow.
    """

    slow_gateway = _ScriptedGateway([])
    orch = Orchestrator(
        lanes={
            Lane.SLOW: LaneConfig(
                llm=slow_gateway,
                system_prompt="SLOW PROMPT",
            ),
        },
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )

    with pytest.raises(UnknownLaneError) as excinfo:
        orch.run(
            query="anything",
            claims=claims,
            request_id="r-unknown-lane",
            lane=Lane.FAST,
        )
    assert excinfo.value.lane is Lane.FAST


def test_constructor_rejects_missing_slow_lane(
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """The slow lane is required — a config that omits it is a wiring
    bug and should fail fast at construction, not on the first request
    that happens to default to slow.
    """

    fast_gateway = _ScriptedGateway([])
    with pytest.raises(ValueError, match="SLOW lane"):
        Orchestrator(
            lanes={
                Lane.FAST: LaneConfig(
                    llm=fast_gateway,
                    system_prompt="FAST PROMPT",
                    tool_names=_FAST_LANE_TOOLS,
                ),
            },
            registry=registry,
            verifier=verifier,
            sessions=sessions,
        )
