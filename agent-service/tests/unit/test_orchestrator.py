"""Unit tests for the orchestrator's tool-use loop.

The LLM gateway is stubbed: we hand the orchestrator a deterministic
sequence of :class:`LlmTurn` objects and assert it dispatches the right
tools, hands the right results back, and produces the expected
:class:`AgentResponse`.

The tests exercise the four orchestration outcomes:

* Happy path with one tool call → verified response.
* Out-of-panel patient_id from the model → ``UNAUTHORIZED`` abstention.
* Tool raising a non-RBAC error → ``TOOL_FAILURE`` abstention.
* Final JSON failing schema validation twice → ``VERIFICATION_FAILED``.
* Fabricated source_id in the final draft → ``VERIFICATION_FAILED`` via
  the verification middleware.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

import pytest

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.abstention import AbstentionState
from clinical_copilot.verification.middleware import VerificationMiddleware


class _RecordingAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ScriptedGateway:
    """Returns a pre-recorded list of turns in order.

    Each ``complete`` call pops the next turn off the queue. Tests that
    want to assert the messages the orchestrator built can read
    ``self.calls``.
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
        role="physician",
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


def test_happy_path_returns_verified_response(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    final_json = (
        '{"cards":[{"title":"Active problems","kind":"problems",'
        '"source_ids":["Condition/p101-cond-1"]}],'
        '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
        '"source_id":"Condition/p101-cond-1"}]}'
    )
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(final_json),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
    )

    response = orch.run(
        query="What problems does this patient have?", claims=claims, request_id="r1"
    )

    assert response.abstention is None
    assert len(response.prose) == 1
    assert response.prose[0].source_id == "Condition/p101-cond-1"
    assert len(response.tool_results) == 1
    assert response.tool_results[0].tool_name == "get_problems"


def test_out_of_panel_tool_call_returns_unauthorized_abstention(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "999"})),
            # If the orchestrator did not short-circuit, it would fall
            # through to here and the test assertion below would still
            # catch the bug — but we should never get this far.
            _final_text_turn('{"cards":[],"prose":[]}'),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="evil cross-patient query", claims=claims, request_id="r2")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.UNAUTHORIZED
    # The orchestrator must short-circuit on RBAC denial — only one LLM
    # call should have happened (the one that emitted the bad tool_use).
    assert len(gateway.calls) == 1


def test_unknown_tool_returns_tool_failure_abstention(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_unobtanium", input={"patient_id": "101"})),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="anything", claims=claims, request_id="r3")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.TOOL_FAILURE


def test_fabricated_source_id_in_draft_yields_verification_failed(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    fabricated_json = (
        '{"cards":[],'
        '"prose":[{"text":"Imaginary condition.",'
        '"source_id":"Condition/never-fetched"}]}'
    )
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(fabricated_json),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="anything", claims=claims, request_id="r4")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED


def test_schema_violation_retries_once_then_aborts(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    bad_json = "this is not json at all"
    bad_json_again = '{"not":"a draft"}'  # missing required fields
    gateway = _ScriptedGateway(
        [
            _final_text_turn(bad_json),
            _final_text_turn(bad_json_again),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="anything", claims=claims, request_id="r5")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "schema validation" in response.abstention.reason
    # Two LLM calls — the original and the one retry.
    assert len(gateway.calls) == 2


def test_max_turns_exceeded_returns_tool_failure(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
) -> None:
    # Construct a gateway that always asks for another tool call. With
    # max_turns=2 we expect a TOOL_FAILURE abstention after the second.
    looping_turns = [
        _tool_use_turn(ToolUse(id=f"tu-{i}", name="get_problems", input={"patient_id": "101"}))
        for i in range(3)
    ]
    gateway = _ScriptedGateway(looping_turns)
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        system_prompt="(test prompt)",
        max_turns=2,
    )

    response = orch.run(query="anything", claims=claims, request_id="r6")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.TOOL_FAILURE
    assert "did not converge" in response.abstention.reason
