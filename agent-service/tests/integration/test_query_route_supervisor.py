"""Integration tests for ``/api/agent/query`` routed through the W2 Supervisor.

The supervisor branch (``main.py:query_route``) only fires when:
- ``Settings.use_supervisor`` is True (default), AND
- ``body.lane == Lane.SLOW`` (fast lane stays on v1 for SLO reasons), AND
- ``AppState.supervisor_*`` fields are all populated (production wiring).

These tests inject the supervisor wiring onto the existing test AppState
(built with ``fixture_store=`` so the v1 fallback path keeps working) via
``object.__setattr__`` — same pattern existing routes use to swap in
test-only collaborators on the frozen dataclass.

What we're guarding here:

1. Flag ON + slow lane + wiring → supervisor handles, prose carries the
   citation anchor walked from the first handoff's chunk.
2. Flag OFF → v1 Orchestrator runs, returns its usual cards/prose.
3. Evidence-only query → ``evidence_partial`` was called, response is
   anchored at a corpus citation.
4. Supervisor raises → silent fallback to v1 (fail-soft).
5. Fast lane → supervisor never invoked even with flag on.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.main import create_app
from clinical_copilot.orchestrator import supervisor as supervisor_mod
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.tools.fixtures import FixtureStore

HMAC_SECRET = "x" * 64


# --------------------------------------------------------------- helpers


class _RecordingAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ScriptedV1Gateway:
    """Drives the v1 Orchestrator's LLM gateway for fallback-path tests."""

    def __init__(self, turns: Sequence[LlmTurn]) -> None:
        self._turns: deque[LlmTurn] = deque(turns)

    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
    ) -> LlmTurn:
        if not self._turns:
            raise AssertionError("v1 scripted gateway exhausted")
        return self._turns.popleft()


@dataclass
class _FakeToolUseBlock:
    """Stand-in for ``anthropic.types.ToolUseBlock`` (mirrors the pattern in
    ``tests/integration/test_supervisor.py``). The supervisor uses
    ``isinstance(block, ToolUseBlock)`` to detect dispatches; we
    monkey-patch that import in each test that needs it."""

    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeMessage:
    content: list[Any]


def _build_supervisor_client(messages: list[_FakeMessage]) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = messages
    return client


def _settings(*, use_supervisor: bool = True) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        hmac_secret=HMAC_SECRET,
        llm_api_key="test-not-used",
        fhir_base_url="http://localhost:0",
        database_url="sqlite:///:memory:",
        audit_salt="test-salt",
        oauth_client_id="cid",
        oauth_private_key_pem=b"",
        oauth_key_id="",
        oauth_token_url="http://localhost:0/token",
        model_slow="test-model-slow",
        model_fast="test-model-fast",
        internal_token="test-internal-token",
        use_supervisor=use_supervisor,
    )


def _mint_jwt(*, patient_id: str = "101") -> str:
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


_V1_FINAL_JSON = (
    '{"cards":[{"title":"Active problems","kind":"problems",'
    '"source_ids":["Condition/p101-cond-1"]}],'
    '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
    '"source_id":"Condition/p101-cond-1"}]}'
)


def _build_client_with_supervisor(
    *,
    use_supervisor: bool,
    sup_client: MagicMock | None,
    intake_fn,
    evidence_fn,
    v1_gateway: _ScriptedV1Gateway,
    audit: _RecordingAudit,
) -> TestClient:
    """Build an AppState that exercises the supervisor branch when
    ``sup_client`` is provided, with v1 Orchestrator wired as the
    fallback so the route still has somewhere to land."""

    settings = _settings(use_supervisor=use_supervisor)
    state = build_app_state(
        settings,
        llm=v1_gateway,
        audit=audit,
        fixture_store=FixtureStore.from_file(),
    )
    # Inject supervisor wiring on top of the test AppState. The frozen
    # dataclass blocks ordinary assignment; ``object.__setattr__`` is
    # the documented escape hatch (also used in test_metrics_route.py).
    object.__setattr__(state, "supervisor_anthropic", sup_client)
    object.__setattr__(state, "supervisor_intake_extractor", intake_fn)
    object.__setattr__(state, "supervisor_evidence_retriever", evidence_fn)
    object.__setattr__(
        state, "supervisor_model", "test-supervisor-model" if sup_client else None,
    )
    app = create_app(settings, state=state)
    return TestClient(app)


@pytest.fixture
def audit() -> _RecordingAudit:
    return _RecordingAudit()


# --------------------------------------------------------------- tests


def test_query_route_uses_supervisor_when_flagged(audit: _RecordingAudit, monkeypatch) -> None:
    """Flag ON + supervisor wiring → response is the supervisor's
    synthesized text anchored at the first handoff's chunk_id."""

    monkeypatch.setattr(supervisor_mod, "ToolUseBlock", _FakeToolUseBlock)

    evidence_calls: list[dict[str, Any]] = []

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {
            "query": kwargs.get("query", ""),
            "chunks": [
                {
                    "chunk_id": "uspstf-lung-2023#chunk-2",
                    "source_doc_id": "uspstf-lung-2023",
                    "title": "Lung Cancer Screening",
                    "text": "Annual low-dose CT for adults 50–80...",
                    "citation": {
                        "chunk_id": "uspstf-lung-2023#chunk-2",
                        "source_doc_id": "uspstf-lung-2023",
                    },
                },
            ],
            "hybrid_enabled": True,
            "reranked": True,
        }

    sup_client = _build_supervisor_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="tu-evidence",
                        name="dispatch_evidence_retriever",
                        input={"query": "lung cancer screening"},
                    ),
                ],
            ),
            _FakeMessage(
                content=[
                    _FakeTextBlock(text="Annual low-dose CT recommended (USPSTF Grade B)."),
                ],
            ),
        ],
    )

    client = _build_client_with_supervisor(
        use_supervisor=True,
        sup_client=sup_client,
        intake_fn=lambda **k: {"facts": {}, "citations": []},
        evidence_fn=fake_evidence,
        v1_gateway=_ScriptedV1Gateway([]),  # never called on this path
        audit=audit,
    )

    token = _mint_jwt()
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What screenings for a 55yo smoker?", "lane": "slow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"] is None
    # Synthesized text from the supervisor lands as a single CitedClaim
    # anchored at the first handoff's chunk_id. ``citation`` is None
    # here because the anchor is a guideline chunk, not a chart record;
    # the response adapter only resolves PatientChartCitations from the
    # chart pack today (guideline citations live on tool_results.records
    # and are surfaced to the UI through that path, not on the cited
    # claim itself).
    assert body["prose"] == [
        {
            "text": "Annual low-dose CT recommended (USPSTF Grade B).",
            "source_id": "uspstf-lung-2023#chunk-2",
            "source_field": None,
            "expected_value": None,
            "citation": None,
        },
    ]
    # No cards: the supervisor's prose only cites the corpus chunk
    # ``uspstf-lung-2023#chunk-2`` — no chart source_ids appear in
    # the text. Per the W2-08 card-filtering rule, cards are emitted
    # only when the supervisor's prose grounds a claim on that
    # topic's source_ids; a corpus-only answer surfaces zero cards
    # so the UI doesn't show "Recent labs" boxes for a question
    # about lung-cancer screening.
    assert body["cards"] == []
    assert body["tool_results"] == []
    # Evidence worker was called exactly once with the supervisor's query.
    assert evidence_calls == [{"query": "lung cancer screening"}]
    # Chart-pack pre-fetch (W2-08) now drives one tool dispatch per
    # topic on every supervisor request, each writing its own audit
    # row through the existing :class:`PatientScopedToolRegistry`.
    # That closes the W2-07 PHI-audit gap: every chart access on the
    # supervisor branch is logged the same way as on v1.
    audit_resource_types = sorted({event.resource_type for event in audit.events})
    assert audit_resource_types == [
        "get_allergies",
        "get_labs",
        "get_meds",
        "get_notes",
        "get_problems",
        "get_visits",
    ]
    assert all(event.action == "SUCCESS" for event in audit.events)
    # Every audit row carries the same request_id so an operator
    # tracing one request can pull the full PHI-access fan-out.
    assert {event.request_id for event in audit.events} == {
        event.request_id for event in audit.events
    }
    assert len({event.request_id for event in audit.events}) == 1


def test_query_route_supervisor_off_uses_v1_orchestrator(audit: _RecordingAudit) -> None:
    """Flag OFF → v1 Orchestrator runs, supervisor client is never invoked."""

    sup_client = MagicMock()

    v1_gateway = _ScriptedV1Gateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(_V1_FINAL_JSON),
        ],
    )

    client = _build_client_with_supervisor(
        use_supervisor=False,
        sup_client=sup_client,
        intake_fn=lambda **k: {"facts": {}, "citations": []},
        evidence_fn=lambda **k: {"chunks": []},
        v1_gateway=v1_gateway,
        audit=audit,
    )

    token = _mint_jwt()
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?"},
    )

    assert response.status_code == 200
    body = response.json()
    # v1 shape: cards populated, prose anchored at a chart resource.
    assert body["abstention"] is None
    assert body["cards"][0]["title"] == "Active problems"
    assert body["prose"][0]["source_id"] == "Condition/p101-cond-1"
    # Supervisor client was never called.
    sup_client.messages.create.assert_not_called()
    # v1 audit row landed.
    assert len(audit.events) == 1
    assert audit.events[0].action == "SUCCESS"


def test_query_route_evidence_only_query(audit: _RecordingAudit, monkeypatch) -> None:
    """A query that fires only ``dispatch_evidence_retriever`` round-trips
    through the route with the partial called once and the response
    anchored at the corpus chunk id."""

    monkeypatch.setattr(supervisor_mod, "ToolUseBlock", _FakeToolUseBlock)

    evidence_calls: list[dict[str, Any]] = []

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {
            "query": kwargs.get("query", ""),
            "chunks": [
                {
                    "chunk_id": "uspstf-htn-2024#chunk-1",
                    "source_doc_id": "uspstf-htn-2024",
                    "citation": {"chunk_id": "uspstf-htn-2024#chunk-1"},
                },
            ],
            "hybrid_enabled": True,
            "reranked": True,
        }

    sup_client = _build_supervisor_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="tu-1",
                        name="dispatch_evidence_retriever",
                        input={"query": "hypertension screening"},
                    ),
                ],
            ),
            _FakeMessage(content=[_FakeTextBlock(text="Screen all adults ≥18 (USPSTF).")]),
        ],
    )

    client = _build_client_with_supervisor(
        use_supervisor=True,
        sup_client=sup_client,
        intake_fn=lambda **k: {"facts": {}, "citations": []},
        evidence_fn=fake_evidence,
        v1_gateway=_ScriptedV1Gateway([]),
        audit=audit,
    )

    token = _mint_jwt()
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What does USPSTF say about hypertension screening?", "lane": "slow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prose"][0]["source_id"] == "uspstf-htn-2024#chunk-1"
    assert evidence_calls == [{"query": "hypertension screening"}]


def test_query_route_supervisor_error_falls_back_to_v1(audit: _RecordingAudit) -> None:
    """Supervisor raising mid-loop falls through to v1 Orchestrator —
    the response is the v1 shape, not a 500."""

    sup_client = MagicMock()
    sup_client.messages.create.side_effect = RuntimeError("upstream Anthropic 503")

    v1_gateway = _ScriptedV1Gateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(_V1_FINAL_JSON),
        ],
    )

    client = _build_client_with_supervisor(
        use_supervisor=True,
        sup_client=sup_client,
        intake_fn=lambda **k: {"facts": {}, "citations": []},
        evidence_fn=lambda **k: {"chunks": []},
        v1_gateway=v1_gateway,
        audit=audit,
    )

    token = _mint_jwt()
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?", "lane": "slow"},
    )

    assert response.status_code == 200
    body = response.json()
    # Fell back to v1 — cards populated, prose anchored at a chart resource.
    assert body["cards"][0]["title"] == "Active problems"
    assert body["prose"][0]["source_id"] == "Condition/p101-cond-1"
    # Supervisor client was hit (and raised) before fallback.
    sup_client.messages.create.assert_called()


def test_query_route_fast_lane_skips_supervisor(audit: _RecordingAudit) -> None:
    """Fast-lane requests bypass the supervisor branch entirely — even
    with the flag on, body.lane=fast routes through v1 Orchestrator
    so the side panel keeps its ≤5s p50 budget."""

    sup_client = MagicMock()

    v1_gateway = _ScriptedV1Gateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"})),
            _final_text_turn(_V1_FINAL_JSON),
        ],
    )

    client = _build_client_with_supervisor(
        use_supervisor=True,
        sup_client=sup_client,
        intake_fn=lambda **k: {"facts": {}, "citations": []},
        evidence_fn=lambda **k: {"chunks": []},
        v1_gateway=v1_gateway,
        audit=audit,
    )

    token = _mint_jwt()
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?", "lane": "fast"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cards"][0]["title"] == "Active problems"
    sup_client.messages.create.assert_not_called()
