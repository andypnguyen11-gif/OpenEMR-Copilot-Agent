"""End-to-end FHIR fetch against a real OpenEMR.

PR 6 acceptance: each FHIR resource the agent reads round-trips against
real demo data. The unit suite covers parsing and retry logic; this test
covers what only a live server enforces — the wire-format details that
even a faithful mock can't simulate (resource shapes the agent assumed
but OpenEMR doesn't actually populate, scope-vs-search-param mismatches,
the lab-category filter actually narrowing the result set, etc.).

Skipped by default. To run:

    OPENEMR_INTEGRATION=1 \\
    OAUTH_CLIENT_ID=... \\
    OAUTH_PRIVATE_KEY_PEM="$(cat path/to/private_key.pem)" \\
    OAUTH_KEY_ID=... \\
    OAUTH_TOKEN_URL=https://openemr.example.com/oauth2/default/token \\
    FHIR_BASE_URL=https://openemr.example.com/apis/default/fhir \\
    OPENEMR_TEST_PATIENT_ID=<fhir-uuid> \\
    uv run pytest tests/integration/test_fhir_client.py -m integration

Each search asserts only that the call succeeds and returns a list of the
expected typed model — the count and contents depend on the demo data and
would make this test brittle. The Patient-by-id check is stricter because
the caller supplies the id directly.
"""

from __future__ import annotations

import os

import httpx
import pytest

from clinical_copilot.auth.oauth_client import OAuthClient
from clinical_copilot.data.fhir_client import FhirClient
from clinical_copilot.data.models import (
    AllergyIntolerance,
    Condition,
    DocumentReference,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
)

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
        pytest.skip(f"{name} not set — required for FHIR integration test")
    return value


@pytest.fixture
def fhir_base_url() -> str:
    return _required_env("FHIR_BASE_URL")


@pytest.fixture
def token_url() -> str:
    return _required_env("OAUTH_TOKEN_URL")


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


async def test_round_trip_each_resource(
    fhir_base_url: str,
    token_url: str,
    client_id: str,
    private_key_pem: bytes,
    key_id: str,
    patient_id: str,
) -> None:
    """The whole point of PR 6: every resource the agent reads survives
    the OAuth + FHIR round-trip on a real OpenEMR.

    Single test rather than seven so we share one OAuth client (and its
    cached token) across all calls — re-minting an assertion per
    resource would multiply the wall-clock time without testing
    anything new.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
        oauth = OAuthClient(
            token_url=token_url,
            client_id=client_id,
            private_key_pem=private_key_pem,
            key_id=key_id,
            http_client=http,
        )
        fhir = FhirClient(
            base_url=fhir_base_url,
            oauth=oauth,
            http_client=http,
        )

        patient = await fhir.get_patient(patient_id)
        assert isinstance(patient, Patient)
        assert patient.id == patient_id

        conditions = await fhir.search_conditions(patient_id=patient_id)
        assert all(isinstance(c, Condition) for c in conditions)

        meds = await fhir.search_medications(patient_id=patient_id)
        assert all(isinstance(m, MedicationRequest) for m in meds)

        allergies = await fhir.search_allergies(patient_id=patient_id)
        assert all(isinstance(a, AllergyIntolerance) for a in allergies)

        labs = await fhir.search_lab_observations(patient_id=patient_id)
        assert all(isinstance(o, Observation) for o in labs)

        encounters = await fhir.search_encounters(patient_id=patient_id)
        assert all(isinstance(e, Encounter) for e in encounters)

        documents = await fhir.search_document_references(patient_id=patient_id)
        assert all(isinstance(d, DocumentReference) for d in documents)
