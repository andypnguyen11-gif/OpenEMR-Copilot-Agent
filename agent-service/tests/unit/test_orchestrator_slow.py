"""Unit tests for the slow-lane orchestrator's tool-use loop.

The LLM gateway is stubbed: we hand the orchestrator a deterministic
sequence of :class:`LlmTurn` objects and assert it dispatches the right
tools, hands the right results back, and produces the expected
:class:`AgentResponse`.

Coverage:

* Happy path with one tool call → verified response.
* Out-of-panel patient_id from the model → ``UNAUTHORIZED`` abstention.
* Tool raising a non-RBAC error → ``TOOL_FAILURE`` abstention.
* Final JSON failing schema validation twice → ``VERIFICATION_FAILED``.
* Fabricated source_id in the final draft → ``VERIFICATION_FAILED`` via
  the verification middleware.
* Multi-turn continuity: turn 2 with the same ``session_id`` sees turn
  1's user message + tool round + final assistant in its ``messages``.
* Schema-retry isolation: turn 1 retry traffic does not appear in turn
  2's restored context.
* Cross-principal isolation: turn 2 with a different JWT sees an empty
  history even when the prior session_id is replayed.
* Per-key lock dropped on uncaught exception so the next request on
  the same session returns immediately.
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
from clinical_copilot.orchestrator.sessions import SessionStore
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


def test_happy_path_returns_verified_response(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
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
        sessions=sessions,
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
    sessions: SessionStore,
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
        sessions=sessions,
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
    sessions: SessionStore,
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
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="anything", claims=claims, request_id="r3")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.TOOL_FAILURE


def test_fabricated_source_id_in_draft_yields_verification_failed(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
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
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="anything", claims=claims, request_id="r4")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED


def test_schema_violation_retries_once_then_aborts(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
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
        sessions=sessions,
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
    sessions: SessionStore,
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
        sessions=sessions,
        system_prompt="(test prompt)",
        max_turns=2,
    )

    response = orch.run(query="anything", claims=claims, request_id="r6")

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.TOOL_FAILURE
    assert "did not converge" in response.abstention.reason


# ---- Multi-turn / session-history coverage ----------------------------


def _final_text_problems_card(source_id: str = "Condition/p101-cond-1") -> str:
    return (
        f'{{"cards":[{{"title":"Active problems","kind":"problems",'
        f'"source_ids":["{source_id}"]}}],'
        f'"prose":[{{"text":"Type 2 diabetes mellitus is on the active problem list.",'
        f'"source_id":"{source_id}"}}]}}'
    )


def test_response_carries_canonical_session_id_when_none_supplied(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(_final_text_problems_card()),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    response = orch.run(query="problems?", claims=claims, request_id="r-canonical")

    assert response.session_id, "first turn must surface a server-canonical session id"
    assert response.abstention is None


def test_multi_turn_continues_session(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """Turn 2 with the same session_id must carry forward turn 1's
    user message, the tool round, and the final assistant turn."""

    gateway = _ScriptedGateway(
        [
            # Turn 1: tool call, then final text.
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(_final_text_problems_card()),
            # Turn 2: model goes straight to a final answer (no tool call)
            # — we just need to inspect the messages it received.
            _final_text_turn('{"cards":[],"prose":[]}'),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    first = orch.run(query="active problems?", claims=claims, request_id="r-1")
    assert first.abstention is None
    sid = first.session_id

    second = orch.run(
        query="any of those new since last visit?",
        claims=claims,
        request_id="r-2",
        session_id=sid,
    )
    assert second.abstention is None
    assert second.session_id == sid, "echoing a known session_id must keep the same canonical id"

    # Inspect the messages handed to the LLM on turn 2's first call.
    turn2_messages = gateway.calls[2]["messages"]
    roles = [m["role"] for m in turn2_messages]
    # Expected: turn-1 user, turn-1 assistant tool_use, turn-1 user
    # (tool_result), turn-1 assistant final text, turn-2 user.
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert turn2_messages[0]["content"] == "active problems?"
    assert turn2_messages[-1]["content"] == "any of those new since last visit?"


def test_schema_retry_traffic_does_not_persist_into_session_history(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """Turn 1 retries past a bad-JSON turn before producing a valid
    final answer. Turn 2 must NOT see the retry frames in its
    messages — only the validated user/assistant pair."""

    bad_json = "this is not json at all"
    gateway = _ScriptedGateway(
        [
            # Turn 1: invalid JSON → retry → valid.
            _final_text_turn(bad_json),
            _final_text_turn('{"cards":[],"prose":[]}'),
            # Turn 2: another final-text turn so we can inspect messages.
            _final_text_turn('{"cards":[],"prose":[]}'),
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    first = orch.run(query="anything", claims=claims, request_id="r-retry-1")
    assert first.abstention is None
    sid = first.session_id

    orch.run(query="another?", claims=claims, request_id="r-retry-2", session_id=sid)

    # gateway.calls indices: 0 = turn-1 first call (got bad JSON), 1 =
    # turn-1 retry (got valid JSON), 2 = turn-2 first call.
    turn2_messages = gateway.calls[2]["messages"]

    # The corrective retry frames are easy to spot: their content
    # mentions the schema-violation prompt or the raw bad-JSON string.
    serialized = repr(turn2_messages)
    assert "did not match the required JSON schema" not in serialized, (
        "retry-traffic corrective prompt leaked into turn 2 history"
    )
    assert bad_json not in serialized, "retry-traffic bad assistant text leaked into turn 2 history"

    # And the structure should be the validated turn-1 plus turn-2 user.
    roles = [m["role"] for m in turn2_messages]
    assert roles == ["user", "assistant", "user"], (
        "turn 2 should restore exactly the validated turn-1 pair plus the new user turn; "
        f"got {roles}"
    )


def test_cross_principal_session_id_replay_returns_empty_history(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """A second principal echoing the first principal's session_id must
    NOT inherit the first principal's history (composite-key isolation)."""

    gateway = _ScriptedGateway(
        [
            _final_text_turn('{"cards":[],"prose":[]}'),  # turn 1, dr-patel
            _final_text_turn('{"cards":[],"prose":[]}'),  # turn 2, dr-evil replaying sid
        ]
    )
    orch = Orchestrator(
        llm=gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    first = orch.run(query="dr-patel question", claims=claims, request_id="r-iso-1")
    sid = first.session_id

    attacker = ClinicianClaims(
        user_id="dr-evil",
        role="physician",
        patient_id=claims.patient_id,
        scopes=list(claims.scopes),
        nonce="n-2",
        jti="jti-attacker",
    )
    second = orch.run(
        query="dr-evil replay",
        claims=attacker,
        request_id="r-iso-2",
        session_id=sid,
    )

    assert second.session_id != sid, (
        "store must mint a fresh id when the supplied id doesn't resolve under the new principal"
    )
    turn2_messages = gateway.calls[1]["messages"]
    assert turn2_messages == [{"role": "user", "content": "dr-evil replay"}], (
        "attacker's session must start with an empty history regardless of the replayed id"
    )


def test_session_lock_dropped_on_uncaught_exception(
    claims: ClinicianClaims,
    registry: ToolRegistry,
    verifier: VerificationMiddleware,
    sessions: SessionStore,
) -> None:
    """If an unexpected error escapes the orchestrator, the per-key
    lock must drop via release() so the next request on the same
    session can proceed without deadlocking."""

    class _ExplodingGateway:
        def complete(
            self,
            *,
            system: str,
            tools: Sequence[dict[str, Any]],
            messages: Sequence[dict[str, Any]],
        ) -> LlmTurn:
            raise RuntimeError("simulated SDK failure")

    orch = Orchestrator(
        llm=_ExplodingGateway(),
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )

    sid_seed, _ = sessions.get_or_create(claims, None)
    sessions.release(claims, sid_seed)

    with pytest.raises(RuntimeError, match="simulated SDK failure"):
        orch.run(query="boom", claims=claims, request_id="r-boom", session_id=sid_seed)

    # If the lock leaked, this call would block forever. The fixture
    # SessionStore has no test timeout, so we use a recovery gateway
    # that succeeds — if we can complete the run, the lock dropped.
    recovery_gateway = _ScriptedGateway([_final_text_turn('{"cards":[],"prose":[]}')])
    orch_recover = Orchestrator(
        llm=recovery_gateway,
        registry=registry,
        verifier=verifier,
        sessions=sessions,
        system_prompt="(test prompt)",
    )
    response = orch_recover.run(
        query="recovery",
        claims=claims,
        request_id="r-recover",
        session_id=sid_seed,
    )
    assert response.abstention is None
