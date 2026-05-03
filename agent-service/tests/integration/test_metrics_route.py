"""End-to-end test for the PR 21 internal metrics endpoint.

Drives a full ``/api/agent/query`` through ``create_app`` against a real
:class:`Orchestrator`, real :class:`AuditLogWriter`, real
:class:`MetricsService` — only the LLM and the chart store are stubbed —
and then calls ``GET /api/agent/internal/metrics`` to assert the row the
orchestrator wrote shows up in the summary. The cross-module pin that
matters here:

* the orchestrator collects ``tool_calls`` and ``fired_rule_ids`` correctly
  during a real run;
* :meth:`MetricsService.record` actually persists the row inside the
  request path (not silently dropped);
* :meth:`MetricsService.summarize` joins the new row with the audit-log
  SUCCESS row the same dispatch wrote, and the completeness check ties out
  by construction.

The internal-token gate gets the same 401 coverage the warm/invalidate/flags
routes do — the metric panel may include traffic statistics that an
adversary could exploit (volume, denial rate), so the gate is non-negotiable.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.reader import AuditLogReader
from clinical_copilot.auth.internal_token import INTERNAL_TOKEN_HEADER
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.main import create_app
from clinical_copilot.observability.metrics import MetricsService
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.tools.fixtures import FixtureStore

HMAC_SECRET = "x" * 64
INTERNAL_TOKEN = "internal-" + ("x" * 32)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


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
        internal_token=INTERNAL_TOKEN,
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


def _tool_use_turn(*tool_uses: ToolUse) -> LlmTurn:
    blocks = [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
        for tu in tool_uses
    ]
    return LlmTurn(
        stop_reason="tool_use",
        text="",
        tool_uses=list(tool_uses),
        raw_assistant_blocks=blocks,
    )


def _final_text_turn(json_payload: str) -> LlmTurn:
    return LlmTurn(
        stop_reason="end_turn",
        text=json_payload,
        tool_uses=[],
        raw_assistant_blocks=[{"type": "text", "text": json_payload}],
    )


def _build_client(
    session_factory: sessionmaker[Session],
    gateway: _ScriptedGateway,
) -> TestClient:
    """Wire the production stack against the shared in-memory engine.

    ``audit`` is the real writer (so the SUCCESS row lands in the DB the
    metrics service reads from); the LLM and the FHIR side are stubbed
    via ``llm=`` and ``fixture_store=``. The metrics service is
    constructed from the same ``session_factory`` inside ``build_app_state``
    when ``audit=None`` — but we want the *same* session_factory the
    fixture exposes, so we pass the real writer in and the build path
    leaves ``session_factory`` for the metrics service / audit reader to
    pick up via the engine. We achieve that by passing the writer + a
    matching reader, both anchored to the test's session_factory.
    """

    settings = _settings()
    audit_writer = AuditLogWriter(session_factory=session_factory)
    audit_reader = AuditLogReader(session_factory=session_factory)
    state = build_app_state(
        settings,
        llm=gateway,
        audit=audit_writer,
        audit_reader=audit_reader,
        fixture_store=FixtureStore.from_file(),
    )
    # ``build_app_state`` normally builds its own session_factory only
    # when ``audit`` is None. Tests must rebind the metrics service to
    # the test's shared engine so the row the orchestrator writes is
    # readable by ``summarize`` — otherwise ``MetricsService`` gets
    # ``session_factory=None`` and its writes are silent no-ops.
    test_metrics = MetricsService(session_factory=session_factory)
    object.__setattr__(state, "metrics_service", test_metrics)
    object.__setattr__(state.orchestrator, "_metrics", test_metrics)

    app = create_app(settings, state=state)
    return TestClient(app)


_HAPPY_FINAL_JSON = (
    '{"cards":[{"title":"Active problems","kind":"problems",'
    '"source_ids":["Condition/p101-cond-1"]}],'
    '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
    '"source_id":"Condition/p101-cond-1"}]}'
)


def test_metrics_endpoint_reflects_a_completed_query(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(
                ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"}),
            ),
            _final_text_turn(_HAPPY_FINAL_JSON),
        ]
    )
    client = _build_client(session_factory, gateway)
    token = _mint_jwt(patient_id="101")

    query_response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?"},
    )
    assert query_response.status_code == 200
    assert query_response.json()["abstention"] is None

    metrics_response = client.get(
        "/api/agent/internal/metrics",
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )
    assert metrics_response.status_code == 200
    body = metrics_response.json()

    # One verified outcome row, one tool dispatch, one matching audit
    # SUCCESS row — completeness is ok by construction.
    assert body["verification_outcome_rate"] == {"verified": 1}
    assert body["totals"]["request_count"] == 1
    assert body["totals"]["tool_calls"] == 1
    assert body["totals"]["audit_success_count"] == 1
    assert body["totals"]["audit_unauthorized_count"] == 0
    assert body["audit_completeness"]["ok"] is True
    assert body["audit_completeness"]["drift"] == 0
    # Cache panel is present (the orchestrator path didn't touch the
    # discrepancy cache for this query, but the panel still renders with
    # zeroed counters and a null hit_rate — the dashboard treats the
    # null as "no samples yet" rather than "0% hit rate").
    assert "cache" in body
    assert body["cache"]["samples_since_startup"] == 0
    assert body["cache"]["hit_rate_since_startup"] is None


def test_metrics_endpoint_records_unauthorized_outcome(
    session_factory: sessionmaker[Session],
) -> None:
    """An RBAC denial inside a query must show up as a ``failed`` bucket
    on the verification rate AND as an ``UNAUTHORIZED`` audit row, so the
    operator can correlate "failed answer" with "denial event."
    """

    gateway = _ScriptedGateway(
        [
            _tool_use_turn(
                ToolUse(id="tu-1", name="get_problems", input={"patient_id": "999"}),
            ),
        ]
    )
    client = _build_client(session_factory, gateway)
    token = _mint_jwt(patient_id="101")

    query_response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "Show me a different patient's chart"},
    )
    assert query_response.status_code == 200
    assert query_response.json()["abstention"]["state"] == "UNAUTHORIZED"

    metrics_response = client.get(
        "/api/agent/internal/metrics",
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )
    body = metrics_response.json()
    assert metrics_response.status_code == 200
    assert body["verification_outcome_rate"] == {"failed": 1}
    assert body["abstention_state_distribution"] == {"UNAUTHORIZED": 1}
    assert body["totals"]["audit_unauthorized_count"] == 1
    assert body["rbac_denial_rate"] == 1.0


def test_metrics_endpoint_rejects_missing_internal_token(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = _ScriptedGateway([])
    client = _build_client(session_factory, gateway)
    response = client.get("/api/agent/internal/metrics")
    assert response.status_code == 401


def test_metrics_endpoint_clamps_window_query_param(
    session_factory: sessionmaker[Session],
) -> None:
    """FastAPI's Query validation rejects a window beyond MAX_WINDOW; the
    422 there is the route-side defense. The summary's clamp is the
    last-line defense in case a future caller bypasses validation.
    """

    gateway = _ScriptedGateway([])
    client = _build_client(session_factory, gateway)
    response = client.get(
        "/api/agent/internal/metrics",
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
        params={"window_seconds": 999_999_999},
    )
    assert response.status_code == 422
