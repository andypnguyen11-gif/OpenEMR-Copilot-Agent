"""Per-lane granularity tests for the verification middleware (PR 12).

The granularity contract:

* **Fast lane** — any verification failure collapses the whole response
  to a single ``VERIFICATION_FAILED`` :class:`Abstention`. ``cards``
  and ``prose`` come back empty.
* **Slow lane** — only the offending claim/card is dropped; survivors
  render unchanged. Each drop appears as one
  :class:`ClaimAbstention` in ``dropped_claims``. If every item drops,
  the response escalates back to a response-level abstention so the UI
  doesn't render an empty body with no explanation.
* **FieldCheckError** (programming/model error) — collapses the whole
  response on either lane. A model that invented a field name is
  suspect across all its claims.

Each test builds the ``draft`` and ``tool_results`` by hand — the
middleware is a pure function of those two inputs.
"""

from __future__ import annotations

from clinical_copilot.orchestrator.lanes import Lane
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
            ProblemRecord(
                source_id="Condition/p101-cond-2",
                code="38341003",
                display="Hypertension",
                onset_date="2018-09-01",
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


def _two_problem_draft() -> ModelDraft:
    """One good claim, one citing a fabricated source_id."""

    return ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="The patient has Type 2 diabetes mellitus.",
                source_id="Condition/p101-cond-1",
            ),
            CitedClaim(
                text="The patient also has fictional condition X.",
                source_id="Condition/fabricated",
            ),
        ],
    )


def test_slow_lane_drops_offending_claim_keeps_others() -> None:
    """Slow lane: bad claim removed, good claim renders, sidecar marker
    records the drop. No response-level abstention."""

    middleware = VerificationMiddleware()
    response = middleware.verify(
        draft=_two_problem_draft(),
        tool_results=[_problem_result()],
        lane=Lane.SLOW,
    )

    assert response.abstention is None
    assert len(response.prose) == 1
    assert response.prose[0].source_id == "Condition/p101-cond-1"
    assert len(response.dropped_claims) == 1
    dropped = response.dropped_claims[0]
    assert dropped.source_id == "Condition/fabricated"
    assert dropped.state == AbstentionState.VERIFICATION_FAILED
    assert "unresolved citation" in dropped.reason


def test_fast_lane_one_bad_claim_abstains_whole_response() -> None:
    """Fast lane: one bad claim collapses the entire response. The good
    claim does NOT render — fast lane trades partial-render for latency."""

    middleware = VerificationMiddleware()
    response = middleware.verify(
        draft=_two_problem_draft(),
        tool_results=[_problem_result()],
        lane=Lane.FAST,
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "Condition/fabricated" in response.abstention.reason
    assert response.prose == []
    assert response.cards == []
    assert response.dropped_claims == []


def test_slow_lane_field_mismatch_drops_only_offending_claim() -> None:
    """Slow lane: a field mismatch on one claim drops that claim and
    keeps the rest. The drop's reason names the field and both values."""

    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Most recent A1c is 7.1%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="7.1",
            ),
            CitedClaim(
                text="A1c was 9.4%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="9.4",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_lab_result()],
        lane=Lane.SLOW,
    )

    assert response.abstention is None
    assert len(response.prose) == 1
    assert response.prose[0].expected_value == "7.1"
    assert len(response.dropped_claims) == 1
    dropped = response.dropped_claims[0]
    assert dropped.source_id == "Observation/p101-lab-1"
    assert dropped.source_field == "value"
    assert "9.4" in dropped.reason
    assert "7.1" in dropped.reason


def test_fast_lane_field_mismatch_abstains_whole_response() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="A1c is 9.4%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="9.4",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_lab_result()],
        lane=Lane.FAST,
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "field-value mismatch" in response.abstention.reason
    assert response.prose == []
    assert response.dropped_claims == []


def test_slow_lane_all_claims_dropped_escalates_to_response_abstention() -> None:
    """Slow lane: when filtering leaves nothing renderable, we still
    surface a response-level abstention so the UI doesn't show an empty
    body with no explanation. ``dropped_claims`` carries the per-claim
    detail; ``abstention`` carries the headline."""

    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Imaginary condition A.",
                source_id="Condition/never-fetched-1",
            ),
            CitedClaim(
                text="Imaginary condition B.",
                source_id="Condition/never-fetched-2",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result()],
        lane=Lane.SLOW,
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert response.prose == []
    assert response.cards == []
    assert len(response.dropped_claims) == 2
    sources = {d.source_id for d in response.dropped_claims}
    assert sources == {"Condition/never-fetched-1", "Condition/never-fetched-2"}


def test_slow_lane_drops_card_with_unresolved_source() -> None:
    """A card carrying a fabricated source_id is dropped wholesale on
    the slow lane (per-card granularity), with one drop entry per
    fabricated source so the audit trail captures each forgery."""

    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[
            Card(
                title="Active problems",
                kind="problems",
                source_ids=["Condition/p101-cond-1", "Condition/fabricated"],
            ),
            Card(
                title="Recent labs",
                kind="labs",
                source_ids=["Observation/p101-lab-1"],
            ),
        ],
        prose=[],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result(), _lab_result()],
        lane=Lane.SLOW,
    )

    assert response.abstention is None
    assert len(response.cards) == 1
    assert response.cards[0].title == "Recent labs"
    assert len(response.dropped_claims) == 1
    dropped = response.dropped_claims[0]
    assert dropped.source_id == "Condition/fabricated"
    assert dropped.source_field is None
    assert "Active problems" in dropped.reason


def test_fast_lane_card_with_unresolved_source_abstains() -> None:
    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[
            Card(
                title="Active problems",
                kind="problems",
                source_ids=["Condition/p101-cond-1", "Condition/fabricated"],
            ),
        ],
        prose=[],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result()],
        lane=Lane.FAST,
    )

    assert response.abstention is not None
    assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
    assert "Condition/fabricated" in response.abstention.reason
    assert response.cards == []


def test_unknown_field_collapses_on_either_lane() -> None:
    """FieldCheckError is a programming error, not a data error.
    A model that invented a field name is suspect across all its
    claims, so we collapse the whole response on either lane."""

    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="The patient's mood is great.",
                source_id="Observation/p101-lab-1",
                source_field="mood",  # not a field on LabRecord
                expected_value="great",
            ),
            CitedClaim(
                text="A1c is 7.1%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="7.1",
            ),
        ],
    )

    for lane in (Lane.SLOW, Lane.FAST):
        response = middleware.verify(
            draft=draft,
            tool_results=[_lab_result()],
            lane=lane,
        )
        assert response.abstention is not None, f"lane={lane}"
        assert response.abstention.state == AbstentionState.VERIFICATION_FAILED
        assert "field check rejected" in response.abstention.reason
        assert response.prose == []
        assert response.dropped_claims == []


def test_happy_path_passes_on_either_lane() -> None:
    """When nothing fails, both lanes return the draft unchanged with
    an empty ``dropped_claims`` sidecar."""

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
                text="Patient has T2DM.",
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

    for lane in (Lane.SLOW, Lane.FAST):
        response = middleware.verify(
            draft=draft,
            tool_results=[_problem_result(), _lab_result()],
            lane=lane,
        )
        assert response.abstention is None, f"lane={lane}"
        assert response.dropped_claims == []
        assert len(response.prose) == 2
        assert len(response.cards) == 1


def test_slow_lane_mixed_failures_drops_both_keeps_clean_claim() -> None:
    """Mix one fabricated-source claim and one field-mismatch claim;
    a third clean claim must survive. Each drop produces its own
    sidecar entry so the audit trail attributes each failure
    independently."""

    middleware = VerificationMiddleware()
    draft = ModelDraft(
        cards=[],
        prose=[
            CitedClaim(
                text="Imaginary condition.",
                source_id="Condition/fabricated",
            ),
            CitedClaim(
                text="A1c was 9.4%.",
                source_id="Observation/p101-lab-1",
                source_field="value",
                expected_value="9.4",
            ),
            CitedClaim(
                text="Patient has T2DM.",
                source_id="Condition/p101-cond-1",
            ),
        ],
    )

    response = middleware.verify(
        draft=draft,
        tool_results=[_problem_result(), _lab_result()],
        lane=Lane.SLOW,
    )

    assert response.abstention is None
    assert len(response.prose) == 1
    assert response.prose[0].source_id == "Condition/p101-cond-1"
    assert len(response.dropped_claims) == 2
    by_source = {d.source_id: d for d in response.dropped_claims}
    assert "unresolved citation" in by_source["Condition/fabricated"].reason
    assert "field-value mismatch" in by_source["Observation/p101-lab-1"].reason
