"""Fail-closed tests for trace-payload PHI redaction.

CLAUDE.md flags ``PHI redaction to LangSmith`` as a high-risk path that
requires test-first coverage. The contract is brutally simple: PHI from
any tool result, LLM message, or response prose must never appear in the
serialized trace payload that goes over the wire to LangSmith.

Allowlist redaction (build a new dict from known-safe fields), not
denylist (filter known-bad strings) — denylist fails the moment a new
record type adds a free-text field that nobody remembers to scrub. These
tests therefore plant distinctive sentinels in every PHI-bearing
position the trace pipeline could possibly serialize, then assert no
sentinel survives the redactor.
"""

from __future__ import annotations

import json

from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.observability.redaction import (
    _scrub_phi_patterns,
    redact_llm_inputs,
    redact_llm_outputs,
    redact_orchestrator_inputs,
    redact_orchestrator_outputs,
    redact_tool_dispatch_inputs,
    redact_tool_outputs,
)
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.orchestrator.schemas import AgentResponse, Card, CitedClaim
from clinical_copilot.tools.records import NoteRecord, ProblemRecord, ToolResult
from clinical_copilot.verification.abstention import Abstention, AbstentionState

# Distinctive sentinels — if any of these survive a redactor, the test
# fails. Each tags a PHI shape from a different surface so a regression
# narrows quickly.
NOTE_SENTINEL = "PATIENT_NOTE_BODY_SECRET_PHI_ALPHA_42"
PROBLEM_SENTINEL = "Type-2-Diabetes-DISPLAY-LEAK_BETA_99"
QUERY_SENTINEL = "WHAT_IS_THE_PATIENT_BIRTHDATE_GAMMA_77"
PROSE_SENTINEL = "Patient-prose-leakage-DELTA_31"
LLM_TEXT_SENTINEL = "MODEL_DRAFT_TEXT_EPSILON_55"
SYSTEM_PROMPT_SENTINEL = "SYSTEM_PROMPT_PHI_ZETA_18"
TOOL_RESULT_BLOCK_SENTINEL = "INSIDE_TOOL_RESULT_ETA_03"


def _phi_sample_tool_result() -> ToolResult:
    return ToolResult(
        tool_name="get_notes",
        patient_id="101",
        records=[
            NoteRecord(
                source_id="DocumentReference/p101-note-1",
                note_date="2026-04-15",
                author="Dr Patel",
                body=NOTE_SENTINEL,
            ),
            ProblemRecord(
                source_id="Condition/p101-cond-1",
                code="E11.9",
                display=PROBLEM_SENTINEL,
                onset_date=None,
                status="active",
            ),
        ],
    )


def _assert_no_sentinels(payload: object, sentinels: list[str]) -> None:
    """Serialize the payload the way LangSmith would and assert no sentinel survives.

    LangSmith ultimately JSON-serializes whatever the redactor returns;
    we mirror that here with ``default=str`` to catch any object whose
    ``str()`` would expose its underlying fields.
    """

    blob = json.dumps(payload, default=str)
    for sentinel in sentinels:
        assert sentinel not in blob, (
            f"PHI sentinel {sentinel!r} leaked into trace payload: {blob!r}"
        )


def test_tool_outputs_redact_record_bodies_to_structural_metadata() -> None:
    redacted = redact_tool_outputs(_phi_sample_tool_result())

    _assert_no_sentinels(redacted, [NOTE_SENTINEL, PROBLEM_SENTINEL])
    assert redacted["tool_name"] == "get_notes"
    assert redacted["record_count"] == 2
    # source_ids are server-issued opaque identifiers, not PHI: keep
    # them so traces can be joined with audit-log lookups during incident
    # response. patient_id from the result wrapper is replaced with its
    # hash for the same reason — joinable, but not reversible.
    assert "DocumentReference/p101-note-1" in redacted["source_ids"]
    assert "patient_id" not in redacted
    assert len(redacted["patient_id_hash"]) == 64  # sha256 hex


def test_tool_outputs_when_none_returned_yields_empty_metadata() -> None:
    """``process_outputs`` may receive ``None`` if the wrapped function
    raised before returning. The redactor must not crash and must not
    fabricate metadata implying a successful call."""

    redacted = redact_tool_outputs(None)
    assert redacted == {"record_count": 0, "source_ids": []}


def test_tool_dispatch_inputs_drop_raw_patient_id_in_favor_of_hash() -> None:
    inputs = {
        "self": object(),  # langsmith captures bound-method ``self`` — must be dropped
        "name": "get_notes",
        "claims": _claims_with_secrets(),
        "patient_id": "101",
        "request_id": "r1",
    }
    redacted = redact_tool_dispatch_inputs(inputs)

    assert "patient_id" not in redacted
    assert "self" not in redacted
    assert "claims" not in redacted  # full ClinicianClaims must not be passed through
    assert len(redacted["patient_id_hash"]) == 64
    assert redacted["tool_name"] == "get_notes"
    assert redacted["user_id"] == "dr-patel"
    assert redacted["role"] == "physician"
    # JWT replay-defense identifiers (nonce, jti) must never end up in a trace.
    _assert_no_sentinels(redacted, ["jti-secret-must-not-leak", "nonce-secret-must-not-leak"])


def test_llm_inputs_drop_message_content_and_system_prompt() -> None:
    inputs = {
        "self": object(),
        "system": f"You are a clinical assistant. {SYSTEM_PROMPT_SENTINEL}",
        "tools": [
            {"name": "get_problems", "description": "...", "input_schema": {}},
            {"name": "get_notes", "description": "...", "input_schema": {}},
        ],
        "messages": [
            {"role": "user", "content": QUERY_SENTINEL},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-1",
                        "content": json.dumps({"body": TOOL_RESULT_BLOCK_SENTINEL}),
                    }
                ],
            },
        ],
    }
    redacted = redact_llm_inputs(inputs)

    _assert_no_sentinels(
        redacted,
        [QUERY_SENTINEL, SYSTEM_PROMPT_SENTINEL, TOOL_RESULT_BLOCK_SENTINEL],
    )
    assert redacted["message_count"] == 2
    assert redacted["tool_def_names"] == ["get_problems", "get_notes"]
    assert redacted["system_prompt_length"] > 0


def test_llm_outputs_drop_text_content_keep_metadata() -> None:
    turn = LlmTurn(
        stop_reason="end_turn",
        text=LLM_TEXT_SENTINEL,
        tool_uses=[ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})],
        raw_assistant_blocks=[
            {"type": "text", "text": LLM_TEXT_SENTINEL},
            {
                "type": "tool_use",
                "id": "tu-1",
                "name": "get_problems",
                "input": {"patient_id": "101"},
            },
        ],
        input_tokens=1234,
        output_tokens=567,
    )
    redacted = redact_llm_outputs(turn)

    _assert_no_sentinels(redacted, [LLM_TEXT_SENTINEL])
    assert redacted["stop_reason"] == "end_turn"
    assert redacted["tool_use_names"] == ["get_problems"]
    assert redacted["text_length"] == len(LLM_TEXT_SENTINEL)
    assert redacted["usage_metadata"] == {
        "input_tokens": 1234,
        "output_tokens": 567,
        "total_tokens": 1801,
    }


def test_llm_outputs_when_none_returned_yields_empty_metadata() -> None:
    redacted = redact_llm_outputs(None)
    assert redacted == {
        "stop_reason": None,
        "text_length": 0,
        "tool_use_names": [],
        "tool_use_count": 0,
        "usage_metadata": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def test_orchestrator_inputs_drop_query_text() -> None:
    inputs = {
        "self": object(),
        "query": QUERY_SENTINEL,
        "claims": _claims_with_secrets(),
        "request_id": "r1",
    }
    redacted = redact_orchestrator_inputs(inputs)

    _assert_no_sentinels(
        redacted,
        [QUERY_SENTINEL, "jti-secret-must-not-leak", "nonce-secret-must-not-leak"],
    )
    assert redacted["query_length"] == len(QUERY_SENTINEL)
    assert redacted["request_id"] == "r1"
    assert redacted["user_id"] == "dr-patel"
    assert redacted["role"] == "physician"
    assert len(redacted["patient_id_hash"]) == 64


def test_orchestrator_outputs_drop_prose_text_and_card_titles() -> None:
    response = AgentResponse(
        cards=[
            Card(
                title="Active problems",
                kind="problems",
                source_ids=["Condition/p101-cond-1"],
            )
        ],
        prose=[CitedClaim(text=PROSE_SENTINEL, source_id="Condition/p101-cond-1")],
        tool_results=[_phi_sample_tool_result()],
        abstention=None,
    )

    redacted = redact_orchestrator_outputs(response)

    _assert_no_sentinels(redacted, [PROSE_SENTINEL, NOTE_SENTINEL, PROBLEM_SENTINEL])
    assert redacted["card_count"] == 1
    assert redacted["prose_count"] == 1
    assert redacted["tool_result_count"] == 1
    assert redacted["abstention_state"] is None


def test_orchestrator_outputs_capture_abstention_state() -> None:
    response = AgentResponse(
        cards=[],
        prose=[],
        tool_results=[],
        abstention=Abstention(
            state=AbstentionState.UNAUTHORIZED,
            reason="unauthorized access denied at tool 'get_problems'",
        ),
    )
    redacted = redact_orchestrator_outputs(response)
    assert redacted["abstention_state"] == "UNAUTHORIZED"


def test_llm_inputs_surface_model_from_bound_self() -> None:
    """LangSmith captures the bound-method ``self`` for every
    ``@traceable`` instance method, so the ``AnthropicLlmGateway``
    appears in the inputs dict under ``self``. Surfacing its ``model``
    attribute is what lets a trace reader filter by lane (slow lane
    runs the larger model, fast lane the smaller) without descending
    into the parent orchestrator span — and asserting it here pins
    that the public attribute name doesn't drift."""

    class _GatewayLike:
        model = "claude-haiku-4-5-20251001"

    inputs = {
        "self": _GatewayLike(),
        "system": "You are a clinical assistant.",
        "tools": [],
        "messages": [],
    }
    redacted = redact_llm_inputs(inputs)
    assert redacted["model"] == "claude-haiku-4-5-20251001"


def test_llm_inputs_omit_model_when_self_lacks_attribute() -> None:
    """Test stubs in the orchestrator unit tests don't carry a
    ``model`` attribute; the redactor must degrade silently rather
    than reporting a misleading value."""

    inputs = {
        "self": object(),
        "system": "x",
        "tools": [],
        "messages": [],
    }
    redacted = redact_llm_inputs(inputs)
    assert "model" not in redacted


def test_orchestrator_inputs_surface_lane() -> None:
    """The lane (slow / fast) is the single highest-leverage filter for
    a trace reader because it identifies which model + system prompt +
    tool subset ran. Surfacing it on the orchestrator span avoids
    forcing readers to drill into the LLM child span to learn it."""

    inputs = {
        "self": object(),
        "query": "anything",
        "claims": _claims_with_secrets(),
        "request_id": "r1",
        "lane": Lane.FAST,
    }
    redacted = redact_orchestrator_inputs(inputs)
    assert redacted["lane"] == "fast"


def test_orchestrator_inputs_omit_lane_when_absent() -> None:
    """Some test paths construct the inputs dict without a lane; the
    redactor must not emit ``"lane": "None"`` in that case."""

    inputs = {
        "self": object(),
        "query": "anything",
        "claims": _claims_with_secrets(),
        "request_id": "r1",
    }
    redacted = redact_orchestrator_inputs(inputs)
    assert "lane" not in redacted


# --- Regex backstop coverage --------------------------------------------------
#
# The allowlist redactors above drop free-text wholesale. The regex
# backstop is belt-and-suspenders for PHI that slips through inside an
# *allowed* field — e.g. an MRN baked into a model-chosen ``tool_name``,
# an email accidentally promoted to ``user_id``, or a future record
# schema growing a string identifier whose contents weren't audited.
#
# These tests plant each PHI shape inside a normally-allowed field and
# assert the backstop scrubs it to ``[REDACTED:<KIND>]``.


def test_phi_backstop_replaces_ssn() -> None:
    assert _scrub_phi_patterns("contact 123-45-6789") == "contact [REDACTED:SSN]"


def test_phi_backstop_replaces_mrn() -> None:
    assert _scrub_phi_patterns("MRN: 123456") == "[REDACTED:MRN]"
    assert _scrub_phi_patterns("see mrn 99887766") == "see [REDACTED:MRN]"


def test_phi_backstop_replaces_phone() -> None:
    for phone in ("415-555-0100", "(415) 555-0100", "415.555.0100"):
        assert _scrub_phi_patterns(f"call {phone} now") == "call [REDACTED:PHONE] now", phone


def test_phi_backstop_replaces_email() -> None:
    assert (
        _scrub_phi_patterns("ping patient.smith@example.org for results")
        == "ping [REDACTED:EMAIL] for results"
    )


def test_phi_backstop_replaces_dob_us_format() -> None:
    assert _scrub_phi_patterns("DOB 03/14/1972") == "DOB [REDACTED:DOB]"
    assert _scrub_phi_patterns("born 12-25-1985") == "born [REDACTED:DOB]"


def test_phi_backstop_replaces_dob_iso_format() -> None:
    assert _scrub_phi_patterns("dob: 1972-03-14") == "dob: [REDACTED:DOB]"


def test_phi_backstop_replaces_fhir_name_keys() -> None:
    bundle_ish = '{"resourceType":"Patient","name":[{"family":"Smith","given":["John","Q"]}]}'
    scrubbed = _scrub_phi_patterns(bundle_ish)
    assert "Smith" not in scrubbed
    assert "John" not in scrubbed
    assert scrubbed.count("[REDACTED:FHIR_NAME]") == 2


def test_phi_backstop_leaves_clean_text_alone() -> None:
    """Latencies, IDs, and structural metadata must pass through untouched."""

    assert _scrub_phi_patterns("supervisor_dispatch_ms=145") == "supervisor_dispatch_ms=145"
    assert _scrub_phi_patterns("get_problems") == "get_problems"
    assert _scrub_phi_patterns("Condition/p101-cond-1") == "Condition/p101-cond-1"


def test_phi_backstop_fires_on_phi_inside_tool_name_via_redactor() -> None:
    """If a future tool_name ever carries an MRN-shaped fragment, the
    backstop catches it even though the allowlist passed the field
    through unchanged."""

    inputs = {
        "self": object(),
        "name": "lookup MRN: 4567890 for chart pull",
        "claims": _claims_with_secrets(),
        "patient_id": "101",
        "request_id": "r1",
    }
    redacted = redact_tool_dispatch_inputs(inputs)
    assert "4567890" not in redacted["tool_name"]
    assert "[REDACTED:MRN]" in redacted["tool_name"]


def test_phi_backstop_fires_on_email_in_user_id_via_redactor() -> None:
    """``user_id`` can be a clinician identifier; if a downstream
    auth path ever fills it with an email-shaped value, the backstop
    must redact it before the trace goes over the wire."""

    claims = ClinicianClaims(
        user_id="dr.patel@example.org",
        role=Role.PHYSICIAN,
        patient_id="101",
        scopes=["system/Condition.read"],
        nonce="n",
        jti="j",
    )
    redacted = redact_orchestrator_inputs(
        {
            "self": object(),
            "query": "anything",
            "claims": claims,
            "request_id": "r1",
        }
    )
    assert redacted["user_id"] == "[REDACTED:EMAIL]"


def test_phi_backstop_fires_on_dob_in_request_id_via_redactor() -> None:
    """``request_id`` is opaque server data; a DOB-shaped value embedded
    in it (e.g. via a debug helper that concatenated a date) must still
    be scrubbed."""

    inputs = {
        "self": object(),
        "name": "get_problems",
        "claims": _claims_with_secrets(),
        "patient_id": "101",
        "request_id": "trace-1972-03-14-abc",
    }
    redacted = redact_tool_dispatch_inputs(inputs)
    assert "1972-03-14" not in redacted["request_id"]
    assert "[REDACTED:DOB]" in redacted["request_id"]


def test_phi_backstop_walks_lists_in_redactor_output() -> None:
    """``tool_def_names`` and ``message_roles`` are lists of strings; the
    backstop's recursive walker must enter list elements, not just
    scalar string fields."""

    inputs = {
        "self": object(),
        "system": "ok",
        "tools": [
            {"name": "get_problems", "description": "...", "input_schema": {}},
            {"name": "lookup MRN 999000 helper", "description": "...", "input_schema": {}},
        ],
        "messages": [],
    }
    redacted = redact_llm_inputs(inputs)
    assert "999000" not in redacted["tool_def_names"][1]
    assert "[REDACTED:MRN]" in redacted["tool_def_names"][1]


def _claims_with_secrets() -> ClinicianClaims:
    """Real ``ClinicianClaims`` instance carrying sentinel ``nonce`` /
    ``jti`` values — the redactor must never pass either through."""

    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id="101",
        scopes=["system/Condition.read", "system/DocumentReference.read"],
        nonce="nonce-secret-must-not-leak",
        jti="jti-secret-must-not-leak",
    )
