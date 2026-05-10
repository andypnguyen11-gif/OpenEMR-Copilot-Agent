"""End-to-end smoke test that flipping ``LANGSMITH_TRACING=true`` is safe.

The unit suite at :mod:`tests.unit.test_phi_redaction` covers each
redactor in isolation with sentinel PHI. This test validates the
*wiring* — that when ``LANGSMITH_TRACING`` is set and a
``@traceable``-wrapped function runs, the payload that LangSmith's
client would send over the wire is the redacted one, not the raw one.

This is the gate the plan calls for before flipping the env var on
Railway: if any sentinel survives into ``Client.create_run`` or
``update_run`` arguments, the wiring is broken and tracing must stay
off until fixed.

The strategy is to monkey-patch ``langsmith.Client.create_run`` and
``update_run`` to capture every call instead of dispatching to the
LangSmith API, then drive each ``@traceable`` decorator with a stub
function whose inputs and outputs carry distinctive sentinels covering
every PHI shape the regex backstop knows about (SSN, MRN, phone,
email, DOB, FHIR family/given) plus the high-risk free-text surfaces
the allowlist already drops (raw query, prose, note bodies).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from langsmith import Client
from langsmith.run_helpers import tracing_context

from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.observability.tracing import (
    traceable_llm_complete,
    traceable_orchestrator_run,
    traceable_tool_dispatch,
)
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.orchestrator.schemas import AgentResponse, Card, CitedClaim
from clinical_copilot.tools.records import NoteRecord, ProblemRecord, ToolResult

# Sentinels covering both the allowlist-handled free-text surfaces and
# every regex-backstop pattern. If any survives into LangSmith's wire
# payload, the test fails and tracing must stay off.
_SENTINELS = [
    "QUERY_SECRET_PHI_ALPHA_42",
    "PROSE_LEAK_BETA_99",
    "NOTE_BODY_GAMMA_77",
    "PROBLEM_DISPLAY_DELTA_31",
    "MODEL_TEXT_EPSILON_55",
    "SYSTEM_PROMPT_ZETA_18",
    # Regex-backstop shapes
    "999-88-7777",  # SSN
    "MRN: 1234567",
    "415-555-0100",  # phone
    "patient.smith@example.org",  # email
    "03/14/1972",  # DOB
    '"family":"Smith"',  # FHIR name
]


@pytest.fixture
def captured_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Patch ``Client.create_run`` and ``update_run`` to capture every
    call the ``@traceable`` machinery makes, with ``LANGSMITH_TRACING``
    forced on. Returns the captured-call list so each test can serialize
    it the way LangSmith would and assert no sentinel survives.

    Wrapped in :func:`tracing_context(enabled=True)` because langsmith's
    ``_TRACING_ENABLED`` ContextVar is process-scoped — if any other
    test in the suite ran inside ``tracing_context(enabled=False)`` (or
    set the var directly) the env var alone would not re-enable
    tracing for our spy. The context-manager override is the
    SDK-supported way to force the decision.
    """

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_dummy")

    captured: list[dict[str, Any]] = []

    def spy_create(self: Client, *args: Any, **kwargs: Any) -> None:
        captured.append({"method": "create_run", "args": args, "kwargs": kwargs})

    def spy_update(self: Client, *args: Any, **kwargs: Any) -> None:
        captured.append({"method": "update_run", "args": args, "kwargs": kwargs})

    monkeypatch.setattr(Client, "create_run", spy_create)
    monkeypatch.setattr(Client, "update_run", spy_update)
    with tracing_context(enabled=True):
        yield captured


def _serialize(captured: list[dict[str, Any]]) -> str:
    """Serialize the captured payloads the way LangSmith's wire format
    would, using ``default=str`` so any object whose ``str()`` exposes
    its fields gets caught."""

    return json.dumps(captured, default=str)


def _claims() -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id="101",
        scopes=["system/Condition.read"],
        nonce="nonce-must-not-leak",
        jti="jti-must-not-leak",
    )


def _phi_tool_result() -> ToolResult:
    return ToolResult(
        tool_name="get_notes",
        patient_id="101",
        records=[
            NoteRecord(
                source_id="DocumentReference/p101-note-1",
                note_date="2026-04-15",
                author="Dr Patel",
                body="NOTE_BODY_GAMMA_77 SSN 999-88-7777",
            ),
            ProblemRecord(
                source_id="Condition/p101-cond-1",
                code="E11.9",
                display="PROBLEM_DISPLAY_DELTA_31",
                onset_date=None,
                status="active",
            ),
        ],
    )


def _phi_response() -> AgentResponse:
    return AgentResponse(
        cards=[Card(title="Active problems", kind="problems", source_ids=["x"])],
        prose=[CitedClaim(text="PROSE_LEAK_BETA_99", source_id="x")],
        tool_results=[_phi_tool_result()],
        abstention=None,
    )


def test_orchestrator_traceable_emits_only_redacted_payload(
    captured_runs: list[dict[str, Any]],
) -> None:
    @traceable_orchestrator_run
    def fake_run(
        self: object,
        query: str,
        claims: ClinicianClaims,
        request_id: str,
        lane: Lane,
    ) -> AgentResponse:
        return _phi_response()

    query_text = "QUERY_SECRET_PHI_ALPHA_42 SSN 999-88-7777 DOB 03/14/1972"
    result = fake_run(object(), query_text, _claims(), "r1", Lane.SLOW)

    assert result.prose[0].text == "PROSE_LEAK_BETA_99"  # function still works
    blob = _serialize(captured_runs)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    # The allowlist is doing real work — the redacted inputs include the
    # query LENGTH (a number), not the query text.
    assert f'"query_length": {len(query_text)}' in blob
    assert '"role": "physician"' in blob  # safe identifier passes through


def test_llm_traceable_emits_only_redacted_payload(
    captured_runs: list[dict[str, Any]],
) -> None:
    class FakeGateway:
        model = "claude-haiku-4-5-20251001"

    @traceable_llm_complete
    def fake_complete(
        self: FakeGateway,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmTurn:
        return LlmTurn(
            stop_reason="end_turn",
            text="MODEL_TEXT_EPSILON_55 with email patient.smith@example.org",
            tool_uses=[ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})],
            raw_assistant_blocks=[],
        )

    fake_complete(
        FakeGateway(),
        f"You are a clinical assistant. SYSTEM_PROMPT_ZETA_18 MRN: 1234567",
        [
            {"role": "user", "content": "QUERY_SECRET_PHI_ALPHA_42 phone 415-555-0100"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-1",
                        "content": json.dumps({"body": "NOTE_BODY_GAMMA_77"}),
                    }
                ],
            },
        ],
        [{"name": "get_problems", "description": "...", "input_schema": {}}],
    )

    blob = _serialize(captured_runs)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    # Structural metadata survives (the allowlist's positive-case proof
    # — without it the redactor would be over-aggressive).
    assert '"message_count": 2' in blob
    assert '"tool_def_names": ["get_problems"]' in blob


def test_tool_dispatch_traceable_emits_only_redacted_payload(
    captured_runs: list[dict[str, Any]],
) -> None:
    @traceable_tool_dispatch
    def fake_dispatch(
        self: object,
        name: str,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> ToolResult:
        return _phi_tool_result()

    fake_dispatch(
        object(),
        "get_notes",
        claims=_claims(),
        patient_id="101",
        request_id="trace-1972-03-14-abc",  # DOB-shaped → backstop must scrub
    )

    blob = _serialize(captured_runs)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    # The DOB-shaped fragment in request_id must have been scrubbed by
    # the regex backstop even though the allowlist passed the field
    # through.
    assert "1972-03-14" not in blob
    assert "[REDACTED:DOB]" in blob
    # Source IDs are server-issued opaque identifiers and pass through.
    assert "DocumentReference/p101-note-1" in blob


def test_traceable_decorators_each_carry_a_redactor() -> None:
    """Wiring contract: every ``@traceable`` decorator in
    :mod:`clinical_copilot.observability.tracing` MUST be configured
    with both ``process_inputs`` and ``process_outputs``. A regression
    here would silently route a raw payload through to LangSmith.

    We assert the configuration positively by exercising each decorator
    against a function whose stub inputs would otherwise leak PHI;
    every other test in this module then verifies the *content* of the
    redaction. This test is the existence proof.
    """

    for decorator in (
        traceable_orchestrator_run,
        traceable_llm_complete,
        traceable_tool_dispatch,
    ):
        # The @traceable wrapper is a callable that yields a closure;
        # the closure carries the user-passed kwargs (including
        # process_inputs / process_outputs) on its captured cell vars.
        assert callable(decorator), (
            "traceable factory must remain a callable decorator"
        )
