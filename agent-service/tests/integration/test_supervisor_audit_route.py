"""End-to-end tests for the PR 18 supervisor audit-log read endpoint.

ARCHITECTURE §8.3 / PRD §6: a supervisor reads audit-log entries for the
residents they oversee — and only those residents. The agent service's
side of the contract is two checks against gateway-signed JWT claims:

    1. ``role == SUPERVISOR``;
    2. the requested resident's user_id is in ``claims.supervises``.

Both denials surface as 403 with the same generic body so a malicious
non-supervisor cannot probe-and-classify resident user IDs by comparing
responses. The tests below pin both gates plus the happy-path read,
including the patient-id-hash projection (raw IDs never appear in the
audit table — that property is what makes the response safe to return
to a supervisor at all).

Wiring matches the production path: real :class:`AuditLogWriter` and
:class:`AuditLogReader` against a shared in-memory SQLite engine. The
writer populates rows directly so the test isn't coupled to the
orchestrator path; the reader runs under the route exactly as it would
in production.
"""

from __future__ import annotations

import time
import uuid
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
from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.audit.reader import MAX_PAGE_SIZE, AuditLogReader
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER
from clinical_copilot.config import Settings
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.main import create_app
from clinical_copilot.orchestrator.llm_gateway import LlmTurn
from clinical_copilot.tools.fixtures import FixtureStore

HMAC_SECRET = "x" * 64
AUDIT_SALT = "test-salt"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the schema applied.

    Each test gets a fresh DB so row counts and ordering are pinned to
    what the test itself wrote, not leaked-in fixtures from neighbours.

    ``StaticPool`` + ``check_same_thread=False`` is the standard FastAPI
    testing pattern for ``:memory:`` SQLite: TestClient dispatches the
    HTTP handler off-thread, and the default SQLite pool gives each
    thread its own private DB. Pinning a single shared connection makes
    the schema and the seeded rows visible from the request handler.
    """

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


@pytest.fixture
def audit_writer(session_factory: sessionmaker[Session]) -> AuditLogWriter:
    return AuditLogWriter(session_factory=session_factory)


@pytest.fixture
def audit_reader(session_factory: sessionmaker[Session]) -> AuditLogReader:
    return AuditLogReader(session_factory=session_factory)


def _settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        hmac_secret=HMAC_SECRET,
        llm_api_key="test-not-used",
        fhir_base_url="http://localhost:0",
        database_url="sqlite:///:memory:",
        audit_salt=AUDIT_SALT,
        oauth_client_id="cid",
        oauth_private_key_pem=b"",
        oauth_key_id="",
        oauth_token_url="http://localhost:0/token",
        model_slow="test-model-slow",
        model_fast="test-model-fast",
        internal_token="test-internal-token",
    )


def _mint_jwt(
    *,
    user_id: str,
    role: str,
    supervises: list[str] | None = None,
    patient_id: str = "101",
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60,
        "jti": uuid.uuid4().hex,
        "user_id": user_id,
        "role": role,
        "patient_id": patient_id,
        "scopes": [],
        "nonce": uuid.uuid4().hex,
    }
    if supervises is not None:
        payload["supervises"] = supervises
    return pyjwt.encode(payload, HMAC_SECRET, algorithm=ALGORITHM)


def _client(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> TestClient:
    settings = _settings()
    state = build_app_state(
        settings,
        # Stub LLM — supervisor route never invokes the orchestrator.
        # The build still wires it so the rest of AppState constructs.
        llm=_NoopGateway(),
        audit=audit_writer,
        audit_reader=audit_reader,
        fixture_store=FixtureStore.from_file(),
    )
    app = create_app(settings, state=state)
    return TestClient(app)


class _NoopGateway:
    """Gateway placeholder — supervisor route doesn't call it.

    Signature matches :class:`LlmGateway` so ``build_app_state`` accepts
    it; the body raises so a future regression that routes the
    supervisor endpoint through the orchestrator fails loudly instead of
    silently spending tokens.
    """

    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
    ) -> LlmTurn:
        raise AssertionError("LLM gateway must not be invoked by the supervisor route")


def _write_resident_event(
    audit_writer: AuditLogWriter,
    *,
    resident_user_id: str,
    patient_id: str,
    request_id: str,
) -> None:
    audit_writer.write(
        AuditEvent(
            user_id=resident_user_id,
            role="resident",
            patient_id_hash=hash_patient_id(patient_id, salt=AUDIT_SALT),
            resource_type="get_problems",
            action="SUCCESS",
            request_id=request_id,
        )
    )


def test_supervisor_reads_assigned_resident_log(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    # Two SUCCESS rows for the supervised resident, one row for an
    # unrelated resident. The endpoint must return the two and never
    # leak the third — the per-user filter at the SQL boundary is the
    # only thing keeping cross-resident reads from happening.
    _write_resident_event(
        audit_writer,
        resident_user_id="resident-jones",
        patient_id="101",
        request_id="req-1",
    )
    _write_resident_event(
        audit_writer,
        resident_user_id="resident-jones",
        patient_id="102",
        request_id="req-2",
    )
    _write_resident_event(
        audit_writer,
        resident_user_id="resident-smith",
        patient_id="103",
        request_id="req-3-other",
    )

    token = _mint_jwt(
        user_id="dr-supervisor",
        role="supervisor",
        supervises=["resident-jones"],
    )
    with _client(audit_writer, audit_reader) as client:
        response = client.get(
            "/api/agent/supervisor/audit/resident-jones",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["resident_user_id"] == "resident-jones"
    entries = body["entries"]
    assert len(entries) == 2
    # Newest-first ordering by ts (then id) — the second insert is most
    # recent.
    assert [e["request_id"] for e in entries] == ["req-2", "req-1"]
    # Patient IDs are HMAC-hashed in the table; the route must not
    # un-hash. Comparing against ``hash_patient_id`` here pins that
    # contract — a regression that started returning raw IDs would
    # fail this assertion before any clinician saw them.
    assert entries[0]["patient_id_hash"] == hash_patient_id("102", salt=AUDIT_SALT)
    assert entries[1]["patient_id_hash"] == hash_patient_id("101", salt=AUDIT_SALT)
    # The unrelated resident's row must not appear in either entry.
    other_hash = hash_patient_id("103", salt=AUDIT_SALT)
    assert all(e["patient_id_hash"] != other_hash for e in entries)


def test_supervisor_reading_non_supervised_resident_is_forbidden(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    # The target resident exists and has rows, but the supervisor's JWT
    # does not list them. The acceptance bullet from PR 18 names this
    # exact case — supervisor reading "another clinician's audit log"
    # must reject. 403 + the same generic body the role-gate uses keeps
    # the two denial paths indistinguishable from outside.
    _write_resident_event(
        audit_writer,
        resident_user_id="resident-smith",
        patient_id="999",
        request_id="req-other",
    )

    token = _mint_jwt(
        user_id="dr-supervisor",
        role="supervisor",
        supervises=["resident-jones"],
    )
    with _client(audit_writer, audit_reader) as client:
        response = client.get(
            "/api/agent/supervisor/audit/resident-smith",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "not authorized to read audit log"}


def test_physician_role_cannot_call_supervisor_endpoint(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    # PRD §6 / USERS §1.4: supervisor role is the only one with audit
    # visibility expanded beyond "their own" actions. A physician token
    # — even one whose ``supervises`` list happens to contain the
    # target — must not pass the role gate.
    token = _mint_jwt(
        user_id="dr-patel",
        role="physician",
        supervises=["resident-jones"],
    )
    with _client(audit_writer, audit_reader) as client:
        response = client.get(
            "/api/agent/supervisor/audit/resident-jones",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "not authorized to read audit log"}


def test_supervisor_reading_resident_with_no_rows_returns_empty_list(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    # Empty result is a 200 with ``entries=[]`` — not a 404. 404 would
    # leak existence information ("this resident has no rows" vs "you
    # may not see them"), conflicting with the deny-shape used by the
    # forbidden case above.
    token = _mint_jwt(
        user_id="dr-supervisor",
        role="supervisor",
        supervises=["resident-jones"],
    )
    with _client(audit_writer, audit_reader) as client:
        response = client.get(
            "/api/agent/supervisor/audit/resident-jones",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "resident_user_id": "resident-jones",
        "entries": [],
    }


def test_supervisor_endpoint_validates_pagination_bounds(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    # ``limit`` larger than ``MAX_PAGE_SIZE`` is a 422 from FastAPI's
    # query-param validator — the route's bound and the reader's
    # internal clamp agree on the same number, so the wire-side error
    # is the visible one.
    token = _mint_jwt(
        user_id="dr-supervisor",
        role="supervisor",
        supervises=["resident-jones"],
    )
    with _client(audit_writer, audit_reader) as client:
        response = client.get(
            f"/api/agent/supervisor/audit/resident-jones?limit={MAX_PAGE_SIZE + 1}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422


def test_supervisor_endpoint_requires_a_jwt(
    audit_writer: AuditLogWriter,
    audit_reader: AuditLogReader,
) -> None:
    with _client(audit_writer, audit_reader) as client:
        response = client.get("/api/agent/supervisor/audit/resident-jones")
    assert response.status_code == 401
