"""Integration tests for the chart-pack pre-fetch on the supervisor branch.

Three scenarios pinned here:

1. **Chart-only** — supervisor sees the chart pack and synthesizes a
   chart answer without dispatching either worker. The response
   anchor comes from the chart pack's source_ids.
2. **Chart + corpus** — supervisor sees the chart pack AND dispatches
   ``dispatch_evidence_retriever`` for the guideline half of the
   question. The handoff lookup wins the anchor.
3. **Cross-patient guard** — the supervisor branch now runs
   :func:`cross_patient_check` before the chart-pack fan-out. A query
   that names a patient other than the bound one short-circuits to
   NO_DATA without invoking either the chart-pack pre-fetch or the
   supervisor LLM.

The patient_name_resolver on ``build_app_state(fixture_store=...)``
returns ``None`` by default so the guard becomes passive — these tests
override it via ``object.__setattr__`` so test 3 has a comparator.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from clinical_copilot.app_state import build_app_state
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.main import create_app
from clinical_copilot.orchestrator import supervisor as supervisor_mod
from clinical_copilot.tools.fixtures import FixtureStore

# Reuse the dataclass stand-ins / settings / JWT minter / audit
# recorder from the W2-07 supervisor route tests so the wire shape
# stays in lockstep across PRs.
from tests.integration.test_query_route_supervisor import (
    HMAC_SECRET,
    _FakeMessage,
    _FakeTextBlock,
    _FakeToolUseBlock,
    _RecordingAudit,
    _ScriptedV1Gateway,
    _settings,
)


def _mint_jwt_for(patient_id: str) -> str:
    """Mint an HS256 JWT bound to ``patient_id`` with full chart scopes."""

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60,
        "jti": uuid.uuid4().hex,
        "user_id": "dr-patel",
        "role": "physician",
        "patient_id": patient_id,
        "scopes": [
            "system/Condition.read",
            "system/MedicationRequest.read",
            "system/AllergyIntolerance.read",
            "system/Observation.read",
            "system/Encounter.read",
            "system/DocumentReference.read",
        ],
        "nonce": uuid.uuid4().hex,
    }
    return pyjwt.encode(payload, HMAC_SECRET, algorithm=ALGORITHM)


def _build_client_with_resolver(
    *,
    sup_client: MagicMock,
    intake_fn,
    evidence_fn,
    audit: _RecordingAudit,
    bound_name: str | None = None,
) -> TestClient:
    """Build the test client and (optionally) override the
    :class:`AppState`'s patient_name_resolver so the cross-patient
    guard has a comparator. Default fixture-store wiring leaves the
    resolver as ``lambda _pid: None``."""

    settings = _settings(use_supervisor=True)
    state = build_app_state(
        settings,
        llm=_ScriptedV1Gateway([]),  # never invoked when supervisor handles
        audit=audit,
        fixture_store=FixtureStore.from_file(),
    )
    object.__setattr__(state, "supervisor_anthropic", sup_client)
    object.__setattr__(state, "supervisor_intake_extractor", intake_fn)
    object.__setattr__(state, "supervisor_evidence_retriever", evidence_fn)
    object.__setattr__(state, "supervisor_model", "test-supervisor-model")
    if bound_name is not None:
        object.__setattr__(
            state,
            "patient_name_resolver",
            lambda _pid, _name=bound_name: _name,
        )
    return TestClient(create_app(settings, state=state))


@pytest.fixture
def audit() -> _RecordingAudit:
    return _RecordingAudit()


# --------------------------------------------------------------- tests


def test_supervisor_chart_only_question_anchors_at_chart_pack_source_id(
    audit: _RecordingAudit,
    monkeypatch,
) -> None:
    """Chart pack populated, supervisor emits text with a chart
    source_id, no worker calls. The response anchors at that
    source_id (chart-pack lookup pass)."""

    monkeypatch.setattr(supervisor_mod, "ToolUseBlock", _FakeToolUseBlock)

    intake_calls: list[dict[str, Any]] = []
    evidence_calls: list[dict[str, Any]] = []

    def fake_intake(**kwargs: Any) -> dict[str, Any]:
        intake_calls.append(kwargs)
        return {"document_id": "x", "facts": {}, "citations": []}

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {"query": kwargs["query"], "chunks": []}

    # Single-turn supervisor: no tool_use, just text. The text echoes
    # a source_id from the fixture's pid=101 chart pack so the
    # adapter's chart-pack lookup resolves the anchor.
    sup_text = "Type 2 diabetes is on the active problem list (Condition/p101-cond-1)."
    sup_client = MagicMock()
    sup_client.messages.create.side_effect = [_FakeMessage(content=[_FakeTextBlock(text=sup_text)])]

    client = _build_client_with_resolver(
        sup_client=sup_client,
        intake_fn=fake_intake,
        evidence_fn=fake_evidence,
        audit=audit,
    )

    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {_mint_jwt_for('101')}"},
        json={"query": "what active problems does this patient have?", "lane": "slow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"] is None
    # Anchor came from the chart pack, not a worker handoff. The prose
    # carries the typed PatientChartCitation alongside the source_id —
    # source_id stays the verifier's join key; citation is display
    # metadata the response adapter populates from the matching
    # ChartPackRecord.to_citation().
    assert body["prose"] == [
        {
            "text": sup_text,
            "source_id": "Condition/p101-cond-1",
            "source_field": None,
            "expected_value": None,
            "citation": {
                "source_type": "patient_chart",
                "field_or_chunk_id": "Condition/p101-cond-1",
                "resource_type": "Condition",
                "resource_id": "p101-cond-1",
                "display_summary": (
                    "Type 2 diabetes mellitus (onset=2019-04-12, status=active)"
                ),
            },
        },
    ]
    # Cards are filtered to topics whose source_ids appear in the
    # supervisor's prose (W2-08). The fixture text only cites
    # ``Condition/p101-cond-1``, so only the problems card surfaces
    # — labs / meds / allergies / visits / notes were pre-fetched
    # for context but stay off-screen. This mirrors the fast lane's
    # tool-driven topic selection.
    assert [card["kind"] for card in body["cards"]] == ["problems"]
    problems_card = body["cards"][0]
    assert "Condition/p101-cond-1" in problems_card["source_ids"]
    # ``tool_results`` carries the original Pydantic records so the
    # chat UI's per-record summary renderer has the dose/observed_on/
    # status fields it needs (without this the slow lane would show
    # bare source_id strings instead of full record details).
    assert [tr["tool_name"] for tr in body["tool_results"]] == ["get_problems"]
    assert body["tool_results"][0]["records"]
    assert all(
        rec["source_id"] == "Condition/p101-cond-1"
        or rec["source_id"].startswith("Condition/")
        for rec in body["tool_results"][0]["records"]
    )
    assert intake_calls == []
    assert evidence_calls == []
    # Audit rows landed for every chart-pack topic — closes the
    # W2-07 PHI-audit gap.
    audit_resources = sorted({event.resource_type for event in audit.events})
    assert audit_resources == [
        "get_allergies",
        "get_labs",
        "get_meds",
        "get_notes",
        "get_problems",
        "get_visits",
    ]


def test_supervisor_chart_plus_corpus_dispatches_evidence_retriever(
    audit: _RecordingAudit,
    monkeypatch,
) -> None:
    """Mixed query: supervisor still dispatches evidence_retriever for
    the guideline half of the question. The handoff lookup wins the
    anchor when both a corpus citation and chart source_ids are
    available — the existing behavior is preserved."""

    monkeypatch.setattr(supervisor_mod, "ToolUseBlock", _FakeToolUseBlock)

    evidence_calls: list[dict[str, Any]] = []

    def fake_intake(**_kwargs: Any) -> dict[str, Any]:
        return {"document_id": "x", "facts": {}, "citations": []}

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {
            "query": kwargs["query"],
            "chunks": [{"chunk_id": "ada-2024#1", "source_doc_id": "ada-2024"}],
        }

    sup_client = MagicMock()
    sup_client.messages.create.side_effect = [
        _FakeMessage(
            content=[
                _FakeToolUseBlock(
                    id="tu-evidence",
                    name="dispatch_evidence_retriever",
                    input={"query": "type 2 diabetes management"},
                ),
            ],
        ),
        _FakeMessage(
            content=[
                _FakeTextBlock(
                    text=(
                        "Per ADA 2024 (ada-2024#1), patient with active "
                        "type 2 diabetes (Condition/p101-cond-1) should "
                        "review glycemic control."
                    ),
                ),
            ],
        ),
    ]

    client = _build_client_with_resolver(
        sup_client=sup_client,
        intake_fn=fake_intake,
        evidence_fn=fake_evidence,
        audit=audit,
    )

    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {_mint_jwt_for('101')}"},
        json={
            "query": "what does this patient need for type 2 diabetes management?",
            "lane": "slow",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"] is None
    # Handoff anchor wins because the worker-handoff lookup runs
    # first, ahead of the chart-pack lookup. This preserves the
    # corpus-citation behavior established in W2-07's test #3.
    assert body["prose"][0]["source_id"] == "ada-2024#1"
    assert evidence_calls == [{"query": "type 2 diabetes management"}]
    # Chart-pack audit rows STILL landed — pre-fetch always runs on
    # the supervisor branch regardless of which lookup pass anchors
    # the response.
    audit_resources = sorted({event.resource_type for event in audit.events})
    assert audit_resources == [
        "get_allergies",
        "get_labs",
        "get_meds",
        "get_notes",
        "get_problems",
        "get_visits",
    ]


def test_supervisor_cross_patient_guard_short_circuits(
    audit: _RecordingAudit,
) -> None:
    """``what about Robert's labs?`` against a session bound to
    ``Maria Lopez`` (pid 101) → guard returns NO_DATA without
    invoking the supervisor LLM or fetching the chart pack."""

    sup_client = MagicMock()
    sup_client.messages.create.side_effect = AssertionError(
        "supervisor must not be called when guard fires",
    )

    intake_calls: list[dict[str, Any]] = []
    evidence_calls: list[dict[str, Any]] = []

    def fake_intake(**kwargs: Any) -> dict[str, Any]:
        intake_calls.append(kwargs)
        return {"document_id": "x", "facts": {}, "citations": []}

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {"query": "", "chunks": []}

    client = _build_client_with_resolver(
        sup_client=sup_client,
        intake_fn=fake_intake,
        evidence_fn=fake_evidence,
        audit=audit,
        bound_name="Maria Lopez",
    )

    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {_mint_jwt_for('101')}"},
        json={"query": "what about Robert's labs?", "lane": "slow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"] is not None
    assert body["abstention"]["state"] == "NO_DATA"
    # The guard's reason text mentions the bound patient name so the
    # clinician sees a clear "this session is bound to X" message
    # rather than a generic refusal.
    assert "Maria Lopez" in body["abstention"]["reason"]
    assert body["prose"] == []
    # Supervisor LLM was never invoked.
    sup_client.messages.create.assert_not_called()
    # Workers were never invoked.
    assert intake_calls == []
    assert evidence_calls == []
    # Chart-pack pre-fetch was skipped — no audit rows on a guarded
    # query. (The guard fires before ``build_chart_pack``; if a
    # future regression moved it after, this assertion would catch
    # the leak.)
    assert audit.events == []
