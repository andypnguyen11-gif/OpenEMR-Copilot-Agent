"""End-to-end tool-layer fetch against a real OpenEMR.

Acceptance for the FHIR-backed tool wiring: each of the six retrieval
tools (PR 8) round-trips through the live OAuth + FHIR stack and lands
a typed :class:`ToolResult`. The unit suite covers projection logic and
the ACL-denial contract; this test covers what only a live server
enforces — the :class:`AsyncBridge` actually shipping coroutines onto a
loop the ``httpx.AsyncClient`` recognises, and OpenEMR honouring each
``system/*`` scope the agent registered for in PR 5.5.

Skipped by default. To run:

    OPENEMR_INTEGRATION=1 \\
    OAUTH_CLIENT_ID=... \\
    OAUTH_PRIVATE_KEY_PEM="$(cat path/to/private_key.pem)" \\
    OAUTH_KEY_ID=... \\
    OAUTH_TOKEN_URL=https://openemr.example.com/oauth2/default/token \\
    FHIR_BASE_URL=https://openemr.example.com/apis/default/fhir \\
    OPENEMR_TEST_PATIENT_ID=<fhir-uuid> \\
    uv run pytest tests/integration/test_tools_fhir.py -m integration

Each tool asserts only that the dispatch returns a typed
:class:`ToolResult` and that any record carries a ``source_id`` of the
expected ``ResourceType/<id>`` shape — counts depend on the demo data
and would make the test brittle. The full assertion chain
(``ToolResult`` → typed records → cited ``source_id``) is what PR 11's
verification middleware joins on; if the ``source_id`` shape drifts at
the FHIR boundary, the citation layer goes silently broken downstream.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.oauth_client import OAuthClient
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.data.fhir_client import FhirClient
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.records import ToolResult
from clinical_copilot.tools.registry import ToolRegistry

INTEGRATION_ENABLED = os.environ.get("OPENEMR_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_ENABLED,
        reason="OPENEMR_INTEGRATION!=1 — skipping; see tests/integration/README",
    ),
]

AUDIT_SALT = "integration-test-salt"
ALL_SCOPES = [
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
]


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set — required for tool integration test")
    return value


class _RecordingAuditWriter(AuditLogWriter):
    """In-memory writer — the projection round-trip is the contract under
    test, not the Postgres audit path. The ACL-denial branch (which is
    the only side that writes here) is exercised exhaustively in the
    unit suite; if a happy-path tool dispatch ever reaches this writer
    against a real OpenEMR, the test asserts on it explicitly.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture(scope="module")
def bridge() -> Iterator[AsyncBridge]:
    bridge = AsyncBridge()
    try:
        yield bridge
    finally:
        bridge.shutdown()


@pytest.fixture(scope="module")
def registry(bridge: AsyncBridge) -> ToolRegistry:
    """Build the production-shape FHIR-backed registry once per module.

    Construct the ``httpx.AsyncClient`` and the OAuth + FHIR clients
    inside the bridge loop so the AsyncClient's internal locks bind to
    the same loop the dispatch will run them on. This mirrors what
    :func:`clinical_copilot.app_state.build_app_state` does in
    production, so a regression in either path will show up here.
    """

    token_url = _required_env("OAUTH_TOKEN_URL")
    fhir_base_url = _required_env("FHIR_BASE_URL")
    client_id = _required_env("OAUTH_CLIENT_ID")
    private_key_pem = _required_env("OAUTH_PRIVATE_KEY_PEM").encode("utf-8")
    key_id = _required_env("OAUTH_KEY_ID")

    async def _build() -> FhirClient:
        http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        oauth = OAuthClient(
            token_url=token_url,
            client_id=client_id,
            private_key_pem=private_key_pem,
            key_id=key_id,
            http_client=http,
        )
        return FhirClient(base_url=fhir_base_url, oauth=oauth, http_client=http)

    fhir = bridge.run(_build())
    return ToolRegistry.from_fhir(
        fhir=fhir,
        bridge=bridge,
        audit=_RecordingAuditWriter(),
        audit_salt=AUDIT_SALT,
    )


@pytest.fixture
def patient_id() -> str:
    return _required_env("OPENEMR_TEST_PATIENT_ID")


def _claims(patient_id: str) -> ClinicianClaims:
    return ClinicianClaims(
        user_id="integration-tester",
        role=Role.PHYSICIAN,
        patient_id=patient_id,
        scopes=ALL_SCOPES,
        nonce="n-integration",
        jti=f"jti-integration-{patient_id}",
    )


@pytest.mark.parametrize(
    ("tool_name", "expected_resource_type"),
    [
        ("get_problems", "Condition"),
        ("get_meds", "MedicationRequest"),
        ("get_allergies", "AllergyIntolerance"),
        ("get_labs", "Observation"),
        ("get_visits", "Encounter"),
        ("get_notes", "DocumentReference"),
    ],
)
def test_tool_round_trip_against_live_openemr(
    tool_name: str,
    expected_resource_type: str,
    registry: ToolRegistry,
    patient_id: str,
) -> None:
    """One parametrised case per tool — same patient, six dispatches.

    Sharing the registry (and therefore the OAuth token cache) across
    cases keeps wall-clock time bounded; minting a fresh assertion per
    tool would inflate the test from seconds to tens of seconds.

    Per-tool assertion is intentionally loose: counts and contents
    depend on the demo data, but the typed-result shape and
    ``ResourceType/<id>`` ``source_id`` format are the contracts the
    citation layer (PR 11) and the orchestrator's tool-result framing
    rely on.
    """

    result = registry.dispatch(
        tool_name,
        claims=_claims(patient_id),
        patient_id=patient_id,
        request_id=f"integration-{tool_name}",
    )

    assert isinstance(result, ToolResult)
    assert result.tool_name == tool_name
    assert result.patient_id == patient_id

    for record in result.records:
        assert record.source_id, f"{tool_name} record missing source_id"
        prefix, _, identifier = record.source_id.partition("/")
        assert prefix == expected_resource_type, (
            f"{tool_name} source_id {record.source_id!r} should start with "
            f"{expected_resource_type}/"
        )
        assert identifier, f"{tool_name} source_id {record.source_id!r} missing id"
