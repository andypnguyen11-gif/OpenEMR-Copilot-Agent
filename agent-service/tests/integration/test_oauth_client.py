"""End-to-end OAuth2 + FHIR fetch against a real local OpenEMR.

This is the PR 5.5 acceptance check: a real JWT-bearer ``client_assertion``
round-trip to ``${OAUTH_TOKEN_URL}`` returns a usable bearer token, and that
token then fetches a real ``Patient/$id`` from ``${FHIR_BASE_URL}``. If
either leg fails, PR 5.5 isn't done — no amount of unit coverage substitutes
for the wire-format details (RS384 signature acceptance, registered ``kid``
resolution, audience pinning) that only a real OpenEMR enforces.

Skipped by default. To run:

    OPENEMR_INTEGRATION=1 \\
    OAUTH_CLIENT_ID=... \\
    OAUTH_PRIVATE_KEY_PEM="$(cat path/to/private_key.pem)" \\
    OAUTH_KEY_ID=<kid-registered-in-jwks> \\
    OAUTH_TOKEN_URL=http://localhost:8300/oauth2/default/token \\
    FHIR_BASE_URL=http://localhost:8300/apis/default/fhir \\
    OPENEMR_TEST_PATIENT_ID=<fhir-uuid> \\
    uv run pytest tests/integration -m integration

The test patient UUID must already exist in the local install — pick one
from `phpMyAdmin` → ``patient_data`` (the FHIR ID is the row's ``uuid``,
hex-formatted). One-time client registration steps live in
``agent-service/README.md``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from clinical_copilot.auth.oauth_client import OAuthClient

INTEGRATION_ENABLED = os.environ.get("OPENEMR_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_ENABLED,
        reason="OPENEMR_INTEGRATION!=1 — skipping; see tests/integration/README",
    ),
]


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set — required for OAuth integration test")
    return value


@pytest.fixture
def token_url() -> str:
    return _required_env("OAUTH_TOKEN_URL")


@pytest.fixture
def fhir_base_url() -> str:
    return _required_env("FHIR_BASE_URL")


@pytest.fixture
def client_id() -> str:
    return _required_env("OAUTH_CLIENT_ID")


@pytest.fixture
def private_key_pem() -> bytes:
    return _required_env("OAUTH_PRIVATE_KEY_PEM").encode("utf-8")


@pytest.fixture
def key_id() -> str:
    return _required_env("OAUTH_KEY_ID")


@pytest.fixture
def patient_id() -> str:
    return _required_env("OPENEMR_TEST_PATIENT_ID")


async def test_fetches_token_and_then_patient_resource(
    token_url: str,
    fhir_base_url: str,
    client_id: str,
    private_key_pem: bytes,
    key_id: str,
    patient_id: str,
) -> None:
    """The whole point of PR 5.5: JWT-bearer assertion works, FHIR accepts the token.

    This test is the only place we exercise the actual OpenEMR OAuth2
    server. If it fails, the unit suite passing is not enough — the
    contract under test is the live wire format (RS384 signature
    acceptance, ``kid`` resolution against the registered JWK, audience
    enforcement against the token URL).
    """

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        oauth = OAuthClient(
            token_url=token_url,
            client_id=client_id,
            private_key_pem=private_key_pem,
            key_id=key_id,
            http_client=http,
        )

        token = await oauth.get_access_token()
        assert token, "expected a non-empty access token"

        cached = await oauth.get_access_token()
        assert cached == token, "expected cache to return the same token"

        response = await http.get(
            f"{fhir_base_url}/Patient/{patient_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/fhir+json",
            },
        )

    assert response.status_code == 200, (
        f"FHIR Patient fetch failed: status={response.status_code} body={response.text[:300]!r}"
    )
    body = response.json()
    assert body.get("resourceType") == "Patient"
    assert body.get("id") == patient_id
