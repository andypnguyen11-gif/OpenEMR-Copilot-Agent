"""End-to-end fail-closed test for the audit-log write path.

ARCHITECTURE §7 / §8.3 / PR 19 acceptance: a request that would write
PHI back to the clinician must not complete if the corresponding audit
row cannot be persisted. The unit-level guarantee — that
:class:`AuditLogWriter` re-raises commit failures as
:class:`AuditLogWriteError` — lives in
``tests/unit/test_audit_log_failclosed.py``. The tool-boundary guarantee
— that the error escapes the tool's success path rather than being
swallowed alongside the result — lives in
``tests/unit/test_role_enforcement.py::test_success_audit_write_failure_blocks_tool_result``.

This test pins the last hop: a real HTTP request through the deployed
``POST /api/agent/query`` route, with a real :class:`AuditLogWriter`
wired against a sessionmaker whose ``commit()`` raises (the same shape
a Postgres outage presents to the writer), must respond ``500`` with a
generic error body — never a partial response, never any PHI from the
fixture chart the LLM was about to read from.

The failing sessionmaker is the integration-grade replacement for a
stubbed-subclass ``AuditLogWriter``. ``build_app_state(audit=...)``
takes the real writer; the writer's only DB collaborator is the
``session_factory`` callable, so injecting a callable that hands back a
session whose ``commit()`` raises drives the production code paths end
to end without standing up a real failing Postgres instance.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.main import create_app
from clinical_copilot.orchestrator.llm_gateway import LlmTurn, ToolUse
from clinical_copilot.tools.fixtures import FixtureStore

HMAC_SECRET = "x" * 64

# Substrings drawn from the patient-101 block of
# ``agent-service/tests/fixtures/patients.json``. The 500 body must
# contain none of them — anything from this list leaking through means a
# tool result was rendered into the response before the audit failure
# aborted the request, which is exactly the bug PR 19 fails closed
# against.
_PATIENT_101_PHI_FRAGMENTS: tuple[str, ...] = (
    "Maria Lopez",
    "Type 2 diabetes mellitus",
    "Metformin",
    "Lisinopril",
    "Condition/p101-cond-1",
    "MedicationRequest/p101-med-1",
)


class _ScriptedGateway:
    """Minimal stand-in for the Anthropic gateway.

    Replays a pre-canned sequence of :class:`LlmTurn`. The fail-closed
    path only needs one turn — the first ``tool_use`` triggers the tool
    call whose audit write blows up — so the gateway never has to
    return a final-text turn here.
    """

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


def _failing_session_factory() -> MagicMock:
    """Sessionmaker-shaped callable whose session raises on ``commit()``.

    Mirrors the unit fail-closed test's failure shape (OperationalError
    on commit) so this integration test exercises the same exception
    path the production writer is built against. ``rollback()`` and
    ``close()`` stay no-ops so the writer's ``finally`` block can run
    cleanly on the way out.
    """

    failing_session = MagicMock()
    failing_session.commit.side_effect = OperationalError(
        "commit", {}, BaseException("simulated audit DB outage")
    )
    return MagicMock(return_value=failing_session)


def test_audit_db_failure_returns_500_without_phi() -> None:
    # Wire the real AuditLogWriter against the failing factory so the
    # production write path runs unmodified. The orchestrator drives
    # get_problems for the JWT's pinned patient id (101) — the role gate
    # passes, _run reads the fixture chart, and the success-path audit
    # write blows up. The route's AuditLogWriteError handler must
    # translate that into a generic 500 with no chart data attached.
    audit = AuditLogWriter(session_factory=_failing_session_factory())
    gateway = _ScriptedGateway(
        [
            LlmTurn(
                stop_reason="tool_use",
                text="",
                tool_uses=[
                    ToolUse(id="tu-1", name="get_problems", input={"patient_id": "101"}),
                ],
                raw_assistant_blocks=[
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "get_problems",
                        "input": {"patient_id": "101"},
                    },
                ],
            ),
        ]
    )

    settings = _settings()
    state = build_app_state(
        settings,
        llm=gateway,
        audit=audit,
        fixture_store=FixtureStore.from_file(),
    )
    app = create_app(settings, state=state)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/agent/query",
            headers={"Authorization": f"Bearer {_mint_jwt(patient_id='101')}"},
            json={"query": "What problems does this patient have?"},
        )

    assert response.status_code == 500
    # Generic message, no exception details, no chart content. The body
    # is the small JSON FastAPI emits for an HTTPException; matching it
    # exactly pins the contract — a future regression that swaps in
    # ``str(exc)`` would leak the wrapped DB error through __cause__.
    assert response.json() == {"detail": "audit log unavailable"}

    body_text = response.text
    for fragment in _PATIENT_101_PHI_FRAGMENTS:
        assert fragment not in body_text, (
            f"PHI fragment {fragment!r} leaked into the fail-closed response body"
        )
