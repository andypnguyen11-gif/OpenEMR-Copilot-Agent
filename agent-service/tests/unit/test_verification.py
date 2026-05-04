"""Unit tests for the verification middleware.

The middleware is purely a function of (draft, tool_results), so every
test below builds those two inputs by hand. We do not invoke the LLM or
the registry — those are tested separately in ``test_orchestrator.py``.

The tests cover the four contract points:

1. Happy path — every cited source_id resolves; field values match.
2. Fabricated source_id — citation existence rejects.
3. Field mismatch — citation resolves but value disagrees.
4. Unknown field name — claim points at a field the record doesn't have.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clinical_copilot.orchestrator.schemas import Card, CitedClaim, ModelDraft
from clinical_copilot.tools.records import LabRecord, ProblemRecord, ToolResult
from clinical_copilot.verification.abstention import AbstentionState
from clinical_copilot.verification.middleware import VerificationMiddleware


def _problem_result() -> ToolResult:
    return ToolResult(
        tool_name="get_problems",
        patient_id="101",
        records=[
            ProblemRecord(
                source_id="Condition/p101-cond-1",
                code="44054006",
                display="Type 2 diabetes mellitus",
                onset_date="2019-04-12",
                status="active",
            ),
        ],
    )


def _lab_result() -> ToolResult:
    return ToolResult(
        tool_name="get_labs",
        patient_id="101",
        records=[
            LabRecord(
                source_id="Observation/p101-lab-1",
                code="4548-4",
                display="Hemoglobin A1c",
                value="7.1",
                unit="%",
                observed_on="2026-03-14",
                reference_range="<5.7",
            ),
        ],
    )


def test_empty_draft_yields_no_data_abstention() -> None:
    # The slow-lane prompt instructs the model to respond with empty
    # cards + empty prose for the "I cannot answer" cases (cross-patient
    # query, refusal, no relevant data) and promises the orchestrator
    # will surface a NO_DATA abstention. Without this, the chat UI
    # renders "(agent returned no claims and no abstention)" and users
    # mistake it for a hung request.
    middleware = VerificationMiddleware()
    draft = ModelDraft(cards=[], prose=[])

    response = middleware.verify(draft=draft, tool_results=[_problem_result()])

    assert response.abstention is not None
    assert response.abstention.state is AbstentionState.NO_DATA
    assert "rephrasing" in response.abstention.reason
    assert response.cards == []
    assert response.prose == []
    # Tool results pass through so the metrics writer still sees them.
    assert len(response.tool_results) == 1


def test_partial_field_assertion_rejected_at_schema() -> None:
    """source_field without expected_value (or vice versa) is a verification
    bypass: ``field_check.py`` short-circuits when either is None, so a
    partial assertion would silently skip the field check the model asked
    for. The schema rejects partial assertions at parse time."""

    with pytest.raises(ValidationError):
        CitedClaim(
            text="Most recent A1c is 7.1%.",
            source_id="Observation/p101-lab-1",
            source_field="value",
        )

    with pytest.raises(ValidationError):
        CitedClaim(
            text="Most recent A1c is 7.1%.",
            source_id="Observation/p101-lab-1",
            expected_value="7.1",
        )


def test_happy_path_passes_draft_through() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[
            Card(
                title="Active problems",
                kind="problems",
                source_ids=["Condition/p101-cond-1"],
            ),
        ],
        prose=[
            CitedClaim(
                text="The patient has Type 2 diabetes mellitus.",
                source_id="Condition/p101-cond-1",
            ),
            CitedClaim(
                text="Most recent A1c is 7.1%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="7.1",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result(), _lab_result()],
    )

    assert response.abstention is None
    assert len(response.prose) == 2
    assert response.cards == draft.cards


def test_fabricated_source_id_rejects_with_verification_failed() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Imaginary condition cited.",
                source_id="Condition/never-fetched",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result()],
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "Condition/never-fetched" in response.abstention.reason
    assert response.prose == []
    assert response.cards == []


def test_field_value_mismatch_rejects() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="A1c is 9.4% (incorrect — record says 7.1).",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="9.4",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_lab_result()],
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "field-value mismatch" in response.abstention.reason
    assert response.prose == []


def test_unknown_field_name_rejects_with_verification_failed() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Patient's mood is great.",
                source_id="Observation/p101-lab-1",
                source_field="mood",  # not a field on LabRecord
                expected_value="great",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_lab_result()],
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "field check rejected" in response.abstention.reason


def test_card_with_unresolved_source_id_rejects() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[
            Card(
                title="Imaginary problems",
                kind="problems",
                source_ids=["Condition/p101-cond-1", "Condition/fabricated"],
            ),
        ],
        prose=[],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result()],
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "Condition/fabricated" in response.abstention.reason


def test_field_value_match_after_normalization_passes() -> None:
    middleware = VerificationMiddleware()
    # Casefold + strip in field_check accepts equivalent strings.
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Recent A1c displayed in the chart.",
                source_id="Observation/p101-lab-1",
                source_field="display",
                expected_value="  hemoglobin a1c  ",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_lab_result()],
    )

    assert response.abstention is None
