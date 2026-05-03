"""Unit tests for the FHIR R4 client.

The contract under test is what PR 7's tool layer will lean on: every
public method either returns parsed Pydantic models or raises
:class:`FhirError`; transient 5xx / transport errors retry once; 4xx
surfaces immediately; and every request carries a fresh OAuth bearer.

The auth boundary is not the high-risk path here — that's PR 5.5's job.
The high-risk path for PR 6 is **silent data corruption**: a bad parse
that surfaces as ``status=None`` or an empty list would mis-display chart
data to clinicians without obvious failure. So the parse / shape tests
are extensive even for the happy paths.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import ValidationError

from clinical_copilot.auth.oauth_client import OAuthError
from clinical_copilot.data.fhir_client import (
    LAB_CATEGORY,
    FhirClient,
    FhirError,
)
from clinical_copilot.data.models import (
    AllergyIntolerance,
    Condition,
    DocumentReference,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE_URL = "https://openemr.example.test/apis/default/fhir"
PATIENT_ID = "abcd-1234-efgh-5678"


# ---------- shared fixtures ----------


class _StubOAuth:
    """Minimal stand-in for :class:`OAuthClient`.

    Captures every token request so the FHIR client tests can assert that
    the bearer attaches per-call. Constructing a real :class:`OAuthClient`
    here would drag in the JWT minter and a second mock transport for no
    benefit — the FHIR client only consumes ``get_access_token``.
    """

    def __init__(self, token: str = "stub-token") -> None:
        self.token = token
        self.calls = 0
        self.raise_next: OAuthError | None = None

    async def get_access_token(self) -> str:
        self.calls += 1
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return self.token


class _ScriptedTransport:
    """httpx mock transport that replays a queue of canned responses.

    Each test builds the queue explicitly; an empty queue raises so a
    silent over-fetch (e.g. retry firing when it shouldn't) shows up as
    a failed test rather than a stalled request.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._responses: deque[httpx.Response | Exception] = deque()

    def queue(self, response: httpx.Response) -> None:
        self._responses.append(response)

    def queue_exception(self, exc: Exception) -> None:
        self._responses.append(exc)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected request to {request.url}: response queue empty")
        item = self._responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def transport() -> _ScriptedTransport:
    return _ScriptedTransport()


@pytest.fixture
async def http_client(
    transport: _ScriptedTransport,
) -> AsyncIterator[httpx.AsyncClient]:
    mock_transport = httpx.MockTransport(transport)
    async with httpx.AsyncClient(transport=mock_transport) as client:
        yield client


@pytest.fixture
def stub_oauth() -> _StubOAuth:
    return _StubOAuth()


@pytest.fixture
def fhir(
    http_client: httpx.AsyncClient,
    stub_oauth: _StubOAuth,
) -> FhirClient:
    return FhirClient(
        base_url=BASE_URL,
        oauth=stub_oauth,  # type: ignore[arg-type]
        http_client=http_client,
        retry_backoff=timedelta(seconds=0),
    )


def _ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _bundle(resource_type: str, *resources: dict[str, object]) -> dict[str, object]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


def _patient(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "gender": "female",
        "birthDate": "1980-04-12",
    }
    base.update(overrides)
    return base


# ---------- get_patient: happy path + auth attachment ----------


async def test_get_patient_returns_parsed_model(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(_ok(_patient()))

    patient = await fhir.get_patient(PATIENT_ID)

    assert isinstance(patient, Patient)
    assert patient.id == PATIENT_ID
    assert patient.gender == "female"
    assert patient.birth_date == "1980-04-12"
    assert patient.name[0].family == "Doe"
    assert patient.name[0].given == ["Jane"]


async def test_request_carries_bearer_token(
    transport: _ScriptedTransport, fhir: FhirClient, stub_oauth: _StubOAuth
) -> None:
    """Every FHIR call must attach the OAuth bearer; without it OpenEMR
    returns 401 and the agent silently appears to have empty charts.
    """
    transport.queue(_ok(_patient()))

    await fhir.get_patient(PATIENT_ID)

    assert stub_oauth.calls == 1
    auth = transport.requests[0].headers.get("authorization")
    assert auth == f"Bearer {stub_oauth.token}"


async def test_request_carries_fhir_accept_header(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """OpenEMR distinguishes ``application/json`` vs ``application/fhir+json``
    on some endpoints; pin the FHIR-specific accept so a JSON regression
    doesn't silently switch us to a non-FHIR variant.
    """
    transport.queue(_ok(_patient()))

    await fhir.get_patient(PATIENT_ID)

    assert transport.requests[0].headers.get("accept") == "application/fhir+json"


async def test_request_url_is_built_from_base_url(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(_ok(_patient()))

    await fhir.get_patient(PATIENT_ID)

    assert str(transport.requests[0].url) == f"{BASE_URL}/Patient/{PATIENT_ID}"


async def test_base_url_trailing_slash_is_normalised(
    http_client: httpx.AsyncClient,
    stub_oauth: _StubOAuth,
    transport: _ScriptedTransport,
) -> None:
    """Base-URL edge case: callers may or may not include the trailing
    slash, but the joined URL must never have a doubled ``//``.
    """
    client = FhirClient(
        base_url=BASE_URL + "/",
        oauth=stub_oauth,  # type: ignore[arg-type]
        http_client=http_client,
        retry_backoff=timedelta(seconds=0),
    )
    transport.queue(_ok(_patient()))

    await client.get_patient(PATIENT_ID)

    assert str(transport.requests[0].url) == f"{BASE_URL}/Patient/{PATIENT_ID}"


# ---------- error paths ----------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_4xx_raises_immediately_no_retry(
    status: int, transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """4xx is not transient — retrying just adds latency to the failure
    message. Pin no-retry so a future "unify retry policy" refactor
    doesn't accidentally regress.
    """
    transport.queue(httpx.Response(status, text="rejected"))

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert str(status) in str(excinfo.value)
    assert len(transport.requests) == 1


async def test_5xx_retries_once_then_surfaces(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(httpx.Response(503, text="upstream"))
    transport.queue(httpx.Response(503, text="upstream"))

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert "503" in str(excinfo.value)
    assert len(transport.requests) == 2


async def test_5xx_then_200_succeeds(transport: _ScriptedTransport, fhir: FhirClient) -> None:
    """Retry budget should actually save the call — pin the success path
    so a regression that drops the retry loop is caught.
    """
    transport.queue(httpx.Response(503, text="upstream"))
    transport.queue(_ok(_patient()))

    patient = await fhir.get_patient(PATIENT_ID)

    assert patient.id == PATIENT_ID
    assert len(transport.requests) == 2


async def test_transport_error_retries_then_surfaces(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue_exception(httpx.ConnectError("connection refused"))
    transport.queue_exception(httpx.ConnectError("connection refused"))

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert "transport" in str(excinfo.value).lower()
    assert len(transport.requests) == 2


async def test_transport_error_then_200_succeeds(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue_exception(httpx.ConnectError("connection refused"))
    transport.queue(_ok(_patient()))

    patient = await fhir.get_patient(PATIENT_ID)

    assert patient.id == PATIENT_ID


async def test_oauth_failure_translated_to_fhir_error(
    fhir: FhirClient, stub_oauth: _StubOAuth
) -> None:
    """OAuth errors must surface as FhirError so callers (PR 7+) only need
    one ``except`` clause on the data path. Letting OAuthError leak would
    bypass that handler.
    """
    stub_oauth.raise_next = OAuthError("token rejected")

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert "OAuth" in str(excinfo.value) or "token" in str(excinfo.value).lower()


async def test_malformed_json_raises_fhir_error(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(httpx.Response(200, text="not json"))

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert "json" in str(excinfo.value).lower()


async def test_missing_required_id_raises_fhir_error(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """``id`` is required on every resource model. A FHIR response without
    one is corrupt; surfacing it would let downstream code carry a
    record with no stable handle to cite.
    """
    transport.queue(_ok({"resourceType": "Patient", "gender": "female"}))

    with pytest.raises(FhirError) as excinfo:
        await fhir.get_patient(PATIENT_ID)

    assert "Patient" in str(excinfo.value)


async def test_get_patient_rejects_empty_id(fhir: FhirClient) -> None:
    with pytest.raises(ValueError):
        await fhir.get_patient("")


async def test_search_rejects_missing_patient(fhir: FhirClient) -> None:
    """Patient-scoped search is the only supported shape — a request
    without ``patient`` would hit the FHIR server unscoped and could
    leak data outside the JWT's panel. Fail at the boundary.
    """
    with pytest.raises(ValueError):
        await fhir.search_conditions(patient_id="")


async def test_constructor_rejects_empty_base_url(
    http_client: httpx.AsyncClient, stub_oauth: _StubOAuth
) -> None:
    with pytest.raises(ValueError):
        FhirClient(
            base_url="",
            oauth=stub_oauth,  # type: ignore[arg-type]
            http_client=http_client,
        )


async def test_constructor_rejects_negative_backoff(
    http_client: httpx.AsyncClient, stub_oauth: _StubOAuth
) -> None:
    with pytest.raises(ValueError):
        FhirClient(
            base_url=BASE_URL,
            oauth=stub_oauth,  # type: ignore[arg-type]
            http_client=http_client,
            retry_backoff=timedelta(seconds=-1),
        )


# ---------- bundle parsing ----------


async def test_search_returns_empty_list_for_empty_bundle(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(_ok(_bundle("Condition")))

    result = await fhir.search_conditions(patient_id=PATIENT_ID)

    assert result == []


async def test_search_skips_off_type_resources(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """A Bundle may interleave ``OperationOutcome`` entries (for warnings)
    with the requested resource type. Skipping them keeps a single
    OperationOutcome from nuking the rest of an otherwise-successful
    search.
    """
    body = _bundle(
        "Condition",
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "warning"}],
        },
        {
            "resourceType": "Condition",
            "id": "c1",
            "code": {"text": "Hypertension"},
            "clinicalStatus": {"coding": [{"code": "active"}]},
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_conditions(patient_id=PATIENT_ID)

    assert len(result) == 1
    assert result[0].id == "c1"


async def test_search_propagates_parse_error_for_bad_entry(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """One corrupt entry must fail the whole search rather than silently
    dropping it — the alternative would let a partial response surface
    as if some records were missing.
    """
    body = _bundle(
        "Condition",
        # missing required ``id``
        {"resourceType": "Condition", "code": {"text": "x"}},
    )
    transport.queue(_ok(body))

    with pytest.raises(FhirError):
        await fhir.search_conditions(patient_id=PATIENT_ID)


async def test_search_rejects_wrong_resource_type_at_bundle_root(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    transport.queue(_ok({"resourceType": "OperationOutcome", "issue": []}))

    with pytest.raises(FhirError) as excinfo:
        await fhir.search_conditions(patient_id=PATIENT_ID)

    assert "Bundle" in str(excinfo.value)


# ---------- per-resource happy path ----------


async def test_search_conditions_parses_codeable_concept(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "Condition",
        {
            "resourceType": "Condition",
            "id": "c1",
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "I10",
                        "display": "Essential hypertension",
                    }
                ],
                "text": "Hypertension",
            },
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "onsetDateTime": "2018-03-10",
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_conditions(patient_id=PATIENT_ID)

    assert len(result) == 1
    cond: Condition = result[0]
    assert cond.id == "c1"
    assert cond.code is not None
    assert cond.code.preferred_display() == "Hypertension"
    assert cond.code.primary_code() == "I10"
    assert cond.clinical_status is not None
    assert cond.clinical_status.primary_code() == "active"
    assert cond.onset_date_time == "2018-03-10"


async def test_search_medications_parses_codeable_concept_and_dosage(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "MedicationRequest",
        {
            "resourceType": "MedicationRequest",
            "id": "m1",
            "status": "active",
            "medicationCodeableConcept": {"text": "Lisinopril 10 mg tablet"},
            "authoredOn": "2024-09-01",
            "dosageInstruction": [{"text": "1 tablet daily"}],
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_medications(patient_id=PATIENT_ID)

    assert len(result) == 1
    med: MedicationRequest = result[0]
    assert med.id == "m1"
    assert med.status == "active"
    assert med.authored_on == "2024-09-01"
    assert (
        med.medication_codeable_concept is not None
        and med.medication_codeable_concept.preferred_display() == "Lisinopril 10 mg tablet"
    )
    assert med.dosage_instruction[0].text == "1 tablet daily"


async def test_search_medications_tolerates_openemr_empty_dosage_wrapper(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """OpenEMR projects empty dosage as ``dosageInstruction: [[]]``.

    Synthea-imported meds nearly always hit this path because Synthea
    doesn't emit dosage detail. Pinning the validator that drops the
    malformed entry — without this, every meds tool call against
    Synthea-loaded test data abstains as ``TOOL_FAILURE`` instead of
    returning the prescription.
    """
    body = _bundle(
        "MedicationRequest",
        {
            "resourceType": "MedicationRequest",
            "id": "m-no-dosage",
            "status": "active",
            "medicationCodeableConcept": {"text": "Acetaminophen 325 MG Oral Tablet"},
            "dosageInstruction": [[]],  # OpenEMR's malformed empty wrapper
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_medications(patient_id=PATIENT_ID)

    assert len(result) == 1
    med: MedicationRequest = result[0]
    assert med.id == "m-no-dosage"
    # The malformed entry was filtered out, leaving an empty list — not
    # the same as missing, but the tool layer reads it identically.
    assert med.dosage_instruction == []


async def test_search_allergies_parses_reaction(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "AllergyIntolerance",
        {
            "resourceType": "AllergyIntolerance",
            "id": "a1",
            "criticality": "high",
            "code": {"text": "Penicillin"},
            "reaction": [
                {
                    "manifestation": [{"text": "Anaphylaxis"}],
                    "severity": "severe",
                }
            ],
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_allergies(patient_id=PATIENT_ID)

    allergy: AllergyIntolerance = result[0]
    assert allergy.id == "a1"
    assert allergy.criticality == "high"
    assert allergy.code is not None
    assert allergy.code.preferred_display() == "Penicillin"
    assert len(allergy.reaction) == 1
    assert allergy.reaction[0].severity == "severe"
    assert allergy.reaction[0].manifestation[0].preferred_display() == "Anaphylaxis"


async def test_search_lab_observations_parses_value_quantity(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "Observation",
        {
            "resourceType": "Observation",
            "id": "o1",
            "status": "final",
            "code": {"text": "Hemoglobin A1c"},
            "effectiveDateTime": "2025-12-15",
            "valueQuantity": {"value": 7.3, "unit": "%"},
            "referenceRange": [{"text": "4.0-5.6 %"}],
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_lab_observations(patient_id=PATIENT_ID)

    obs: Observation = result[0]
    assert obs.id == "o1"
    assert obs.status == "final"
    assert obs.value_quantity is not None
    assert obs.value_quantity.value == 7.3
    assert obs.value_quantity.unit == "%"
    assert obs.reference_range[0].text == "4.0-5.6 %"


async def test_search_lab_observations_passes_category_filter(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """Lab tool never wants vitals or social history mixed in. Pin the
    category filter at the wire level so a refactor that moves the
    filter elsewhere doesn't silently broaden the surface.
    """
    transport.queue(_ok(_bundle("Observation")))

    await fhir.search_lab_observations(patient_id=PATIENT_ID)

    request = transport.requests[0]
    assert request.url.params.get("category") == LAB_CATEGORY
    assert request.url.params.get("patient") == PATIENT_ID


async def test_search_encounters_parses_period(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "Encounter",
        {
            "resourceType": "Encounter",
            "id": "e1",
            "status": "finished",
            "type": [{"text": "Office Visit"}],
            "period": {"start": "2025-09-20T09:00:00Z"},
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_encounters(patient_id=PATIENT_ID)

    enc: Encounter = result[0]
    assert enc.id == "e1"
    assert enc.status == "finished"
    assert enc.period is not None
    assert enc.period.start == "2025-09-20T09:00:00Z"
    assert enc.type[0].preferred_display() == "Office Visit"


async def test_search_document_references_parses_attachment(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    body = _bundle(
        "DocumentReference",
        {
            "resourceType": "DocumentReference",
            "id": "d1",
            "status": "current",
            "date": "2025-09-20",
            "author": [{"reference": "Practitioner/dr-patel"}],
            "content": [
                {
                    "attachment": {
                        "contentType": "text/plain",
                        "data": "Tm90ZSBib2R5",  # "Note body"
                    }
                }
            ],
        },
    )
    transport.queue(_ok(body))

    result = await fhir.search_document_references(patient_id=PATIENT_ID)

    doc: DocumentReference = result[0]
    assert doc.id == "d1"
    assert doc.date == "2025-09-20"
    assert doc.author[0].reference == "Practitioner/dr-patel"
    assert doc.content[0].attachment is not None
    assert doc.content[0].attachment.content_type == "text/plain"
    assert doc.content[0].attachment.data == "Tm90ZSBib2R5"


# ---------- forward-compatibility ----------


async def test_unknown_top_level_fields_are_ignored(
    transport: _ScriptedTransport, fhir: FhirClient
) -> None:
    """FHIR servers attach bookkeeping (``meta``, ``text``, etc.) the
    agent doesn't read. Tolerate them so a server-side change doesn't
    require a coordinated agent release.
    """
    body = _patient(meta={"versionId": "3"}, text={"status": "generated"})
    transport.queue(_ok(body))

    patient = await fhir.get_patient(PATIENT_ID)

    assert patient.id == PATIENT_ID


async def test_models_are_frozen(transport: _ScriptedTransport, fhir: FhirClient) -> None:
    """The orchestrator passes parsed models through verification +
    record-projection layers; any layer accidentally mutating a field
    would be a hard-to-find data corruption bug. Pin the frozen-ness
    contract.
    """
    transport.queue(_ok(_patient()))

    patient = await fhir.get_patient(PATIENT_ID)

    with pytest.raises(ValidationError):
        patient.gender = "male"
