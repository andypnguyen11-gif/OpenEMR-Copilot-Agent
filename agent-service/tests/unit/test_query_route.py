"""End-to-end test for the M3 ``POST /api/agent/query`` route.

Spins up a real FastAPI ``TestClient`` against the production
:class:`create_app`, but with a stub LLM gateway and an in-memory audit
writer so no Anthropic key or Postgres is required. The test exercises:

* JWT signing (real :class:`JwtSigner` analogue using PyJWT directly so
  the verifier validates against the same shared secret),
* Tool-use loop (real :class:`Orchestrator` against the real fixture
  store and tool registry),
* Verification middleware (real one),
* Audit-log writes for an out-of-panel RBAC denial,
* Schema-shape of the JSON response.

If any of these wires breaks the contract, this test fails — which is
why M3 has a single end-to-end test rather than narrow per-handler
mocks.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.main import create_app
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.tools.fixtures import FixtureStore

HMAC_SECRET = "x" * 64


class _RecordingAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ScriptedGateway:
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
            raise AssertionError("scripted gateway exhausted")
        return self._turns.popleft()


def _settings() -> Settings:
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
    )


def _mint_jwt(*, patient_id: str = "101", scopes: list[str] | None = None) -> str:
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
        "scopes": scopes
        if scopes is not None
        else [
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


@pytest.fixture
def audit() -> _RecordingAudit:
    return _RecordingAudit()


def _client(gateway: _ScriptedGateway, audit: _RecordingAudit) -> TestClient:
    settings = _settings()
    state = build_app_state(
        settings,
        llm=gateway,
        audit=audit,
        fixture_store=FixtureStore.from_file(),
    )
    app = create_app(settings, state=state)
    return TestClient(app)


def test_query_happy_path_returns_verified_response(audit: _RecordingAudit) -> None:
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
    client = _client(gateway, audit)

    token = _mint_jwt(patient_id="101")
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"] is None
    assert body["prose"][0]["source_id"] == "Condition/p101-cond-1"
    assert body["tool_results"][0]["tool_name"] == "get_problems"
    assert audit.events == []


def test_query_unauthorized_writes_audit_and_returns_abstention(
    audit: _RecordingAudit,
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(ToolUse(id="tu-1", name="get_problems", input={"patient_id": "999"})),
        ]
    )
    client = _client(gateway, audit)

    token = _mint_jwt(patient_id="101")
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "Show me a different patient's chart"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstention"]["state"] == "UNAUTHORIZED"
    assert len(audit.events) == 1
    assert audit.events[0].action == "UNAUTHORIZED"
    assert audit.events[0].resource_type == "get_problems"


def test_query_without_token_returns_401(audit: _RecordingAudit) -> None:
    client = _client(_ScriptedGateway([]), audit)
    response = client.post("/api/agent/query", json={"query": "hi"})
    assert response.status_code == 401


def test_query_with_tampered_token_returns_401(audit: _RecordingAudit) -> None:
    client = _client(_ScriptedGateway([]), audit)
    bad = _mint_jwt() + "tamper"
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {bad}"},
        json={"query": "hi"},
    )
    assert response.status_code == 401


def test_query_validates_request_body(audit: _RecordingAudit) -> None:
    client = _client(_ScriptedGateway([]), audit)
    token = _mint_jwt()
    # Empty query violates min_length=1.
    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": ""},
    )
    assert response.status_code == 422
