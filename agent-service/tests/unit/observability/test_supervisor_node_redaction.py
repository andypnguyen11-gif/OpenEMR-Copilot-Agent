"""Unit tests for the LangGraph supervisor-node PHI redactors.

These cover ``redact_supervisor_node_inputs`` and
``redact_supervisor_node_outputs`` in isolation. The wiring test that
each LangGraph node actually goes through these redactors lives in
``test_langsmith_safe.py`` (extended in the same change).

The redactors run on the trace-export hot path and must:

* drop every PHI-bearing free-text field on ``TurnState`` (user_query,
  session.patient_name, sub_queries[*].text, drafts[*].text,
  verdicts[*].rationale, final_response.synthesized_text);
* keep allowlisted structural metadata so the trace stays useful
  (request_id, counts, enum values, usage totals, rerank_backend,
  abstention_reason);
* run every surviving string through the regex backstop so PHI baked
  into an otherwise-allowed field (e.g. an MRN smuggled into
  request_id) gets scrubbed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clinical_copilot.observability.redaction import (
    redact_supervisor_node_inputs,
    redact_supervisor_node_outputs,
)
from clinical_copilot.observability.traces import UsageTotals
from clinical_copilot.orchestrator.state import (
    Citation,
    ClaimType,
    CriticVerdict,
    Draft,
    RejectionReason,
    SubQuery,
    Verdict,
    Worker,
)

# Sentinels deliberately distinct from those in test_langsmith_safe so a
# cross-test leak (one redactor stealing fields from another) is
# obvious in failure output.
_SENTINELS = [
    "USER_QUERY_LEAK_ALPHA",
    "PATIENT_NAME_LEAK_BETA",
    "SUBQUERY_TEXT_LEAK_GAMMA",
    "DRAFT_TEXT_LEAK_DELTA",
    "DRAFT_ABSTAIN_LEAK_EPSILON",  # abstain_reason free-text, not enum
    "VERDICT_RATIONALE_LEAK_ZETA",
    "SYNTHESIZED_TEXT_LEAK_ETA",
    "HISTORY_TURN_LEAK_THETA",
    # Regex-backstop shapes — every surviving string field must be
    # scrubbed of these even when the allowlist passes the field
    # through.
    "999-88-7777",  # SSN
    "MRN: 9876543",
    "415-555-0199",  # phone
    "patient.olivia@example.org",
    "03/14/1972",  # DOB
    '"family":"Smith"',
]


def _phi_state() -> dict[str, Any]:
    """Build a ``TurnState``-shaped dict with PHI sentinels in every
    free-text field. Returned as a plain dict — LangGraph passes
    ``TurnState`` through ``@traceable`` as the underlying TypedDict
    payload, which serializes as a dict.
    """

    return {
        "user_query": "USER_QUERY_LEAK_ALPHA SSN 999-88-7777 DOB 03/14/1972",
        "session": {
            "request_id": "req-1234",
            "patient_id": "101",
            "patient_name": "PATIENT_NAME_LEAK_BETA",
            "history": [
                {"role": "user", "text": "HISTORY_TURN_LEAK_THETA"},
            ],
        },
        "sub_queries": [
            SubQuery(
                id="sq-1",
                text="SUBQUERY_TEXT_LEAK_GAMMA phone 415-555-0199",
                claim_type=ClaimType.CHART_FACT,
                target_worker=Worker.CHART_TOOLS,
            ).model_dump(),
        ],
        "drafts": [
            Draft(
                sub_query_id="sq-1",
                worker=Worker.CHART_TOOLS,
                text="DRAFT_TEXT_LEAK_DELTA email patient.olivia@example.org",
                citations=(
                    Citation(source_id="Condition/p101-cond-1", confidence=0.92),
                ),
                abstain_reason="DRAFT_ABSTAIN_LEAK_EPSILON",
            ).model_dump(),
        ],
        "verdicts": [
            Verdict(
                sub_query_id="sq-1",
                verdict=CriticVerdict.REJECT,
                rejection_reason=RejectionReason.NO_CITATION,
                rationale="VERDICT_RATIONALE_LEAK_ZETA MRN: 9876543",
            ).model_dump(),
        ],
        "retry_counts": {"sq-1": 1},
        "final_response": {
            "synthesized_text": "SYNTHESIZED_TEXT_LEAK_ETA",
            "abstention_reason": None,
            "handoffs": [],
            "iterations": 1,
        },
        "rerank_backend": "cohere",
        "usage_totals": UsageTotals(input_tokens=1527, output_tokens=60),
    }


def _dump(payload: dict[str, Any]) -> str:
    """Serialize the way LangSmith would so any string field anywhere
    in a nested structure becomes searchable. ``default=str`` ensures
    Pydantic models / dataclasses get caught by their ``__str__``.
    """

    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------
# Inputs — node receives ``state`` and ``@traceable`` captures it as
# ``{"state": <dict>}``.
# ---------------------------------------------------------------------


def test_supervisor_node_inputs_drops_all_phi_sentinels() -> None:
    state = _phi_state()
    redacted = redact_supervisor_node_inputs({"state": state})
    blob = _dump(redacted)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"


def test_supervisor_node_inputs_keeps_safe_structural_fields() -> None:
    state = _phi_state()
    redacted = redact_supervisor_node_inputs({"state": state})

    assert redacted["request_id"] == "req-1234"
    # Lengths / counts replace free-text fields.
    assert redacted["user_query_length"] == len(state["user_query"])
    assert redacted["sub_query_count"] == 1
    assert redacted["draft_count"] == 1
    assert redacted["verdict_count"] == 1
    # Allowlisted enum / token-count fields pass through.
    assert redacted["rerank_backend"] == "cohere"
    assert redacted["usage_totals"] == {
        "input_tokens": 1527,
        "output_tokens": 60,
    }
    # retry_counts map is keyed by sub_query_id (uuid hex, not PHI).
    assert redacted["retry_counts"] == {"sq-1": 1}


def test_supervisor_node_inputs_emits_patient_id_hash_not_raw() -> None:
    state = _phi_state()
    redacted = redact_supervisor_node_inputs({"state": state})
    # Raw patient_id never escapes; hashed form is present so a trace
    # can still be joined to the audit log.
    assert "101" not in _dump({"hash": redacted.get("patient_id_hash")})
    assert isinstance(redacted.get("patient_id_hash"), str)
    assert len(redacted["patient_id_hash"]) > 16


def test_supervisor_node_inputs_handles_empty_state() -> None:
    redacted = redact_supervisor_node_inputs({"state": {}})
    assert redacted["sub_query_count"] == 0
    assert redacted["draft_count"] == 0
    assert redacted["verdict_count"] == 0
    assert "user_query_length" not in redacted
    assert "patient_id_hash" not in redacted


def test_supervisor_node_inputs_runs_regex_backstop_on_request_id() -> None:
    redacted = redact_supervisor_node_inputs(
        {
            "state": {
                "session": {"request_id": "req-1972-03-14-abc"},
            }
        }
    )
    blob = _dump(redacted)
    # DOB-shaped fragment must be scrubbed even though request_id
    # passes through the allowlist.
    assert "1972-03-14" not in blob
    assert "[REDACTED:DOB]" in blob


# ---------------------------------------------------------------------
# Outputs — LangGraph nodes return partial state dicts. Each shape
# below mirrors what one of the supervisor nodes actually returns.
# ---------------------------------------------------------------------


def test_supervisor_node_outputs_synthesizer_drops_synthesized_text() -> None:
    output = {
        "final_response": {
            "synthesized_text": "SYNTHESIZED_TEXT_LEAK_ETA email patient.olivia@example.org",
            "abstention_reason": None,
            "handoffs": [],
            "iterations": 1,
        },
        "usage_totals": UsageTotals(input_tokens=1527, output_tokens=60),
    }
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    # Length survives so trace can show "synthesizer produced a 76-char
    # response" without exposing the prose.
    assert redacted["synthesized_text_length"] > 0
    assert redacted["abstention_reason"] is None
    assert redacted["usage_totals"] == {
        "input_tokens": 1527,
        "output_tokens": 60,
    }


def test_supervisor_node_outputs_synthesizer_keeps_abstention_reason() -> None:
    output = {
        "final_response": {
            "synthesized_text": "",
            "abstention_reason": "no_data",
            "handoffs": [],
            "iterations": 0,
        },
    }
    redacted = redact_supervisor_node_outputs(output)
    assert redacted["abstention_reason"] == "no_data"
    assert redacted["synthesized_text_length"] == 0


def test_supervisor_node_outputs_drafts_keeps_structural_fields_only() -> None:
    output = {
        "drafts": [
            Draft(
                sub_query_id="sq-1",
                worker=Worker.CHART_TOOLS,
                text="DRAFT_TEXT_LEAK_DELTA",
                citations=(
                    Citation(source_id="Condition/p101-cond-1", confidence=0.9),
                ),
                abstain_reason=None,
            ).model_dump(),
            Draft(
                sub_query_id="sq-2",
                worker=Worker.EVIDENCE_RETRIEVER,
                text="another DRAFT_TEXT_LEAK_DELTA",
                citations=(
                    Citation(corpus_id="guideline-chunk-42", confidence=0.7),
                ),
                abstain_reason="no_data",
            ).model_dump(),
        ],
    }
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    assert redacted["draft_count"] == 2
    # Worker enum + abstain reason taxonomy survive (both are closed
    # enums with no PHI risk). Source/corpus IDs are server-issued
    # opaque identifiers and pass through, matching the orchestrator
    # redactor's policy.
    assert sorted(redacted["draft_workers"]) == sorted(
        [Worker.CHART_TOOLS.value, Worker.EVIDENCE_RETRIEVER.value]
    )
    assert redacted["draft_abstain_reasons"] == [None, "no_data"]
    assert "Condition/p101-cond-1" in blob
    assert "guideline-chunk-42" in blob


def test_supervisor_node_outputs_verdicts_keeps_taxonomy_drops_rationale() -> None:
    output = {
        "verdicts": [
            Verdict(
                sub_query_id="sq-1",
                verdict=CriticVerdict.REJECT,
                rejection_reason=RejectionReason.NO_CITATION,
                rationale="VERDICT_RATIONALE_LEAK_ZETA SSN 999-88-7777",
            ).model_dump(),
            Verdict(
                sub_query_id="sq-2",
                verdict=CriticVerdict.ACCEPT,
                rejection_reason=None,
                rationale="VERDICT_RATIONALE_LEAK_ZETA",
            ).model_dump(),
        ],
    }
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, f"sentinel {sentinel!r} leaked: {blob!r}"
    assert redacted["verdict_count"] == 2
    assert redacted["verdicts"] == [
        {"verdict": "reject", "rejection_reason": "no_citation"},
        {"verdict": "accept", "rejection_reason": None},
    ]


def test_supervisor_node_outputs_sub_queries_drops_text() -> None:
    output = {
        "sub_queries": [
            SubQuery(
                id="sq-1",
                text="SUBQUERY_TEXT_LEAK_GAMMA",
                claim_type=ClaimType.CHART_FACT,
                target_worker=Worker.CHART_TOOLS,
            ).model_dump(),
            SubQuery(
                id="sq-2",
                text="SUBQUERY_TEXT_LEAK_GAMMA-2",
                claim_type=ClaimType.GUIDELINE,
                target_worker=Worker.EVIDENCE_RETRIEVER,
            ).model_dump(),
        ],
    }
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    assert "SUBQUERY_TEXT_LEAK_GAMMA" not in blob
    assert redacted["sub_query_count"] == 2
    # Claim-type taxonomy survives — used for routing and rubrics.
    assert sorted(redacted["sub_query_claim_types"]) == sorted(
        [ClaimType.CHART_FACT.value, ClaimType.GUIDELINE.value]
    )


def test_supervisor_node_outputs_handles_none() -> None:
    redacted = redact_supervisor_node_outputs(None)
    assert redacted == {}


def test_supervisor_node_outputs_handles_unknown_keys_safely() -> None:
    """Defense in depth: a node returning a key the redactor doesn't
    know about must drop it (allowlist), not pass through."""

    output = {
        "future_field_with_phi": "USER_QUERY_LEAK_ALPHA",
        "drafts": [],
    }
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    assert "USER_QUERY_LEAK_ALPHA" not in blob
    assert "future_field_with_phi" not in redacted


def test_supervisor_node_outputs_runs_regex_backstop_on_rerank_backend() -> None:
    """The allowlist permits ``rerank_backend`` because it's a closed
    enum string. If an attacker (or future bug) shoves PHI into it,
    the regex backstop catches it on the way out."""

    output = {"rerank_backend": "cohere MRN: 9876543"}
    redacted = redact_supervisor_node_outputs(output)
    blob = _dump(redacted)
    assert "9876543" not in blob
    assert "[REDACTED:MRN]" in blob


def test_supervisor_node_outputs_usage_totals_passthrough() -> None:
    output = {"usage_totals": UsageTotals(input_tokens=42, output_tokens=7)}
    redacted = redact_supervisor_node_outputs(output)
    assert redacted["usage_totals"] == {
        "input_tokens": 42,
        "output_tokens": 7,
    }


@pytest.mark.parametrize(
    "non_dict_output",
    [
        "a string",
        42,
        ["a", "list"],
        object(),
    ],
)
def test_supervisor_node_outputs_handles_non_dict_returns(non_dict_output: object) -> None:
    """LangGraph node return types are dicts by contract, but the
    redactor runs on the trace-export hot path and must never raise.
    """

    redacted = redact_supervisor_node_outputs(non_dict_output)
    assert isinstance(redacted, dict)
