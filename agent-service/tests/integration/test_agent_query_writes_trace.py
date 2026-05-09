"""End-to-end test for the PR W2-04 ``agent_traces`` writer.

Drives a full ``/api/agent/query`` through ``create_app`` against a real
:class:`Orchestrator` — only the LLM and the chart store are stubbed —
and asserts that exactly one row lands in ``agent_traces`` with the
columns the dashboard reads. The cross-module pin that matters here:

* the v1 orchestrator path threads the LlmTurn token totals into the
  trace row (the supervisor branch tests live separately because they
  need a corpus retriever);
* :meth:`TracesService.record` actually persists the row inside the
  request path (not silently dropped);
* ``retrieval_hits`` and ``extraction_confidence`` are independently
  nullable — the v1 path writes ``NULL`` for both since it never
  invokes the corpus retriever or the document extractor.

Sibling of ``test_metrics_route.py`` (PR 21) and the document-ingest
trace test deferred for follow-up — both go through the same
:class:`TracesService` instance wired in :mod:`app_state`.
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
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.reader import AuditLogReader
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.db.models import AgentTrace
from clinical_copilot.main import create_app
from clinical_copilot.observability.traces import TracesService
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


_DEFAULT_SCOPES: tuple[str, ...] = (
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
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
        "scopes": list(_DEFAULT_SCOPES),
        "nonce": uuid.uuid4().hex,
    }
    return pyjwt.encode(payload, HMAC_SECRET, algorithm=ALGORITHM)


class _ScriptedGateway:
    """Deterministic LlmGateway stub.

    Returns scripted :class:`LlmTurn` values per call so the v1
    orchestrator runs end-to-end without an Anthropic key. Tokens on the
    LlmTurn drive the ``token_in`` / ``token_out`` columns of the trace
    row — set them on a per-turn basis so the test can pin the
    aggregation.
    """

    model = "test-model-slow"

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


def _tool_use_turn(
    *tool_uses: ToolUse,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> LlmTurn:
    blocks = [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
        for tu in tool_uses
    ]
    return LlmTurn(
        stop_reason="tool_use",
        text="",
        tool_uses=list(tool_uses),
        raw_assistant_blocks=blocks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _final_text_turn(
    json_payload: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> LlmTurn:
    return LlmTurn(
        stop_reason="end_turn",
        text=json_payload,
        tool_uses=[],
        raw_assistant_blocks=[{"type": "text", "text": json_payload}],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _build_client(
    session_factory: sessionmaker[Session],
    gateway: _ScriptedGateway,
) -> TestClient:
    """Wire the production stack against the shared in-memory engine.

    Mirrors ``test_metrics_route._build_client`` — same problem and same
    fix: ``build_app_state`` only constructs its own session_factory
    when ``audit`` is None, so we pass the audit writer + matching
    reader anchored to the test's session_factory and then rebind the
    trace writer to the same factory so the row the orchestrator writes
    is readable from the test's engine.
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
    test_traces = TracesService(session_factory=session_factory)
    object.__setattr__(state, "traces_service", test_traces)
    object.__setattr__(state.orchestrator, "_traces", test_traces)

    app = create_app(settings, state=state)
    return TestClient(app)


_HAPPY_FINAL_JSON = (
    '{"cards":[{"title":"Active problems","kind":"problems",'
    '"source_ids":["Condition/p101-cond-1"]}],'
    '"prose":[{"text":"Type 2 diabetes mellitus is on the active problem list.",'
    '"source_id":"Condition/p101-cond-1"}]}'
)


def test_agent_query_writes_one_trace_row(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = _ScriptedGateway(
        [
            _tool_use_turn(
                ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"}),
                input_tokens=400,
                output_tokens=20,
            ),
            _final_text_turn(
                _HAPPY_FINAL_JSON,
                input_tokens=620,
                output_tokens=180,
            ),
        ],
    )
    client = _build_client(session_factory, gateway)
    token = _mint_jwt(patient_id="101")

    response = client.post(
        "/api/agent/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "What problems does this patient have?"},
    )
    assert response.status_code == 200
    assert response.json()["abstention"] is None

    with Session(session_factory.kw["bind"]) as session:
        rows = session.execute(select(AgentTrace)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "dr-patel"
    assert row.role == "physician"
    assert row.lane == "slow"
    assert row.model_tier == "test-model-slow"
    # Tokens are summed from every successful LlmTurn in the loop.
    assert row.token_in == 400 + 620
    assert row.token_out == 20 + 180
    assert row.latency_ms >= 0
    # v1 orchestrator path: it never invokes the corpus retriever or
    # the document extractor, so both shape-specific columns stay NULL.
    # The supervisor branch is the only call site that populates
    # ``retrieval_hits``; the document-ingest entry point is the only
    # call site that populates ``extraction_confidence``.
    assert row.retrieval_hits is None
    assert row.extraction_confidence is None
