"""Seed deployed OpenEMR with the M5 eval fixture patients.

Reads ``agent-service/tests/fixtures/patients.json`` and POSTs each patient
plus their FHIR resources (Condition, MedicationRequest, AllergyIntolerance,
Observation, Encounter, DocumentReference) against ``${FHIR_BASE_URL}``
using a writer-scoped OAuth client. The fixture's hand-encoded shapes
become the live counterparts the M5 eval cases assert against once
``OPENEMR_TEST_PATIENT_ID`` points at one of the seeded ids.

Why this exists (vs. Synthea bulk-loading or manual UI entry):

- Manual entry: ~5 patients x 7 resource types = a half-day of clicking.
- Synthea: generates *random* patients, but the eval cases assert against
  *specific* shapes (e.g. Maria's duplicate-class duplicate). Random data
  doesn't help for the M5 cases — that's PR 22-23's adversarial-suite
  use case where statistical coverage is the point.
- This script: deterministic, mirrors the fixture exactly, re-runnable.

Idempotency: this script is **not** idempotent — re-running creates a new
duplicate set of patients. The simplest cleanup is to revoke the writer
client (Admin → API Clients → Disable) before a re-run, then revoke any
newly-orphaned patients via the OpenEMR UI. A "search by name and skip"
mode would catch the obvious case but miss races; we'd rather a noisy
duplicate than a silent skip that leaves the eval suite asserting against
stale data.

Usage::

    uv run python scripts/seed_fixture_patients.py \\
        --fhir-base-url https://openemr.example.com/apis/default/fhir \\
        --token-url https://openemr.example.com/oauth2/default/token \\
        --client-id <writer-client-id> \\
        --private-key writer-private-key.pem \\
        --key-id seeder-2026-05 \\
        --out seeded-patient-ids.json

Output: ``seeded-patient-ids.json`` maps each fixture patient name to the
OpenEMR-assigned id (the FHIR uuid). Pick any one for
``OPENEMR_TEST_PATIENT_ID`` when running the integration tests.

Operational notes captured during the first run (update as new quirks
surface):

- OpenEMR's FHIR write endpoint expects camelCase field names; using a
  POSTed Bundle with ``urn:uuid:`` references (Synthea-style) routes through
  a different code path that's only partially supported. Resource-by-
  resource POSTs avoid that ambiguity.
- The ``Patient`` resource accepts ``name``/``birthDate``/``gender`` but
  rejects ``id`` on POST — let OpenEMR assign one and read it from the
  response.
- ``Condition.subject`` and friends use the assigned Patient id, not the
  fixture's internal numeric id. The seeder rewrites refs after the
  Patient POST.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

# Add the agent-service src/ to sys.path so the script can import
# ``clinical_copilot.auth.oauth_client`` when run as ``python scripts/...``.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinical_copilot.auth.oauth_client import OAuthClient  # noqa: E402

WRITE_SCOPES: tuple[str, ...] = (
    "system/Patient.read",
    "system/Patient.write",
    "system/Condition.read",
    "system/Condition.write",
    "system/MedicationRequest.read",
    "system/MedicationRequest.write",
    "system/AllergyIntolerance.read",
    "system/AllergyIntolerance.write",
    "system/Observation.read",
    "system/Observation.write",
    "system/Encounter.read",
    "system/Encounter.write",
    "system/DocumentReference.read",
    "system/DocumentReference.write",
)

SNOMED_SYSTEM = "http://snomed.info/sct"
LOINC_SYSTEM = "http://loinc.org"

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "patients.json"

HTTP_BAD_REQUEST = 400


# ---------- FHIR resource builders ----------


def build_patient(demographics: dict[str, object]) -> dict[str, object]:
    """Map fixture demographics to a FHIR Patient.

    Fixture stores ``age`` rather than birthdate; we synthesize a
    deterministic birthdate (Jan 1 of the appropriate year) so the same
    fixture always produces the same patient on re-seed. Anything more
    realistic would be churn for no test benefit.
    """
    name = str(demographics["name"])
    family, *given = name.split(" ") if " " not in name else _split_name(name)
    age = int(demographics["age"])  # type: ignore[arg-type]
    birth_year = datetime.now(tz=UTC).year - age
    return {
        "resourceType": "Patient",
        "name": [
            {
                "use": "official",
                "family": family,
                "given": given or [name],
                "text": name,
            }
        ],
        "gender": _map_sex(str(demographics["sex"])),
        "birthDate": f"{birth_year}-01-01",
    }


def _split_name(name: str) -> list[str]:
    parts = name.split(" ")
    if not parts:
        return [name]
    family = parts[-1]
    given = parts[:-1]
    return [family, *given]


def _map_sex(sex: str) -> str:
    return {"M": "male", "F": "female"}.get(sex.upper(), "unknown")


def build_condition(patient_ref: str, problem: dict[str, object]) -> dict[str, object]:
    return {
        "resourceType": "Condition",
        "subject": {"reference": patient_ref},
        "clinicalStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": str(problem["status"]),
                }
            ]
        },
        "code": {
            "coding": [
                {
                    "system": SNOMED_SYSTEM,
                    "code": str(problem["code"]),
                    "display": str(problem["display"]),
                }
            ],
            "text": str(problem["display"]),
        },
        "onsetDateTime": str(problem["onset_date"]),
    }


def build_medication_request(patient_ref: str, med: dict[str, object]) -> dict[str, object]:
    return {
        "resourceType": "MedicationRequest",
        "status": str(med["status"]),
        "intent": "order",
        "subject": {"reference": patient_ref},
        "medicationCodeableConcept": {"text": str(med["name"])},
        "authoredOn": str(med["started_on"]),
        "dosageInstruction": [{"text": str(med["dose"])}],
    }


def build_allergy(patient_ref: str, allergy: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {
        "resourceType": "AllergyIntolerance",
        "patient": {"reference": patient_ref},
        "clinicalStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                    "code": "active",
                }
            ]
        },
        "code": {"text": str(allergy["substance"])},
    }
    reaction: dict[str, object] = {}
    if allergy.get("reaction"):
        reaction["manifestation"] = [{"text": str(allergy["reaction"])}]
    if allergy.get("severity"):
        reaction["severity"] = _map_severity(str(allergy["severity"]))
    if reaction:
        body["reaction"] = [reaction]
    return body


def _map_severity(severity: str) -> str:
    """Fixture severities don't always match FHIR's enum; coerce to the
    spec's allowed values (``mild`` / ``moderate`` / ``severe``).
    """
    s = severity.lower()
    if s in {"mild", "moderate", "severe"}:
        return s
    return "moderate"


def build_observation(patient_ref: str, lab: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {
        "resourceType": "Observation",
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory",
                    }
                ]
            }
        ],
        "subject": {"reference": patient_ref},
        "code": {
            "coding": [
                {
                    "system": LOINC_SYSTEM,
                    "code": str(lab["code"]),
                    "display": str(lab["display"]),
                }
            ],
            "text": str(lab["display"]),
        },
        "effectiveDateTime": str(lab["observed_on"]),
    }
    value = lab.get("value")
    unit = lab.get("unit")
    if value is not None:
        try:
            body["valueQuantity"] = {
                "value": float(str(value)),
                "unit": str(unit) if unit else "",
            }
        except ValueError:
            body["valueString"] = str(value)
    if lab.get("reference_range"):
        body["referenceRange"] = [{"text": str(lab["reference_range"])}]
    return body


def build_encounter(patient_ref: str, visit: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {
        "resourceType": "Encounter",
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "AMB",
            "display": "ambulatory",
        },
        "subject": {"reference": patient_ref},
        "type": [{"text": str(visit["encounter_type"])}],
        "period": {"start": _iso_datetime(str(visit["visited_on"]))},
    }
    if visit.get("chief_complaint"):
        body["reasonCode"] = [{"text": str(visit["chief_complaint"])}]
    return body


def _iso_datetime(value: str) -> str:
    """OpenEMR's Encounter parser is happier with a full ISO 8601
    timestamp than a bare date — pad to noon UTC if only a date is given.
    """
    try:
        date.fromisoformat(value)
    except ValueError:
        return value
    return f"{value}T12:00:00Z"


def build_document_reference(patient_ref: str, note: dict[str, object]) -> dict[str, object]:
    body_text = str(note["body"])
    encoded = base64.b64encode(body_text.encode("utf-8")).decode("ascii")
    return {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {"text": "Progress note"},
        "subject": {"reference": patient_ref},
        "date": _iso_datetime(str(note["note_date"])),
        "author": [{"display": str(note["author"])}],
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain",
                    "data": encoded,
                    "title": f"Note from {note.get('author', 'unknown')}",
                }
            }
        ],
    }


# ---------- HTTP layer ----------


class SeedError(RuntimeError):
    """Any failure during the seed run; carries a server-side diagnostic."""


async def _post_resource(
    http: httpx.AsyncClient,
    *,
    fhir_base_url: str,
    token: str,
    resource: dict[str, object],
) -> str:
    resource_type = str(resource["resourceType"])
    url = f"{fhir_base_url.rstrip('/')}/{resource_type}"
    response = await http.post(
        url,
        json=resource,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/fhir+json",
            "Accept": "application/fhir+json",
        },
    )
    if response.status_code >= HTTP_BAD_REQUEST:
        raise SeedError(
            f"POST {resource_type} failed: status={response.status_code} "
            f"body={response.text[:400]!r}"
        )
    # Two ways OpenEMR can communicate the assigned id:
    # - Response body is the created resource (FHIR "return=representation")
    # - Response body is empty + Location header has ``ResourceType/{id}``
    location = response.headers.get("location") or response.headers.get("Location")
    if location and "/" in location:
        # Trim any trailing ``_history/...`` segment per FHIR spec.
        tail = location.rstrip("/").split("/")
        if "_history" in tail:
            idx = tail.index("_history")
            tail = tail[:idx]
        return tail[-1]
    try:
        body = response.json()
    except ValueError as exc:
        raise SeedError(
            f"POST {resource_type} returned no Location and unparsable body: "
            f"{response.text[:200]!r}"
        ) from exc
    # OpenEMR's FHIR write endpoint returns ``{"pid": <int>, "uuid": "<uuid>"}``
    # on 201 instead of the standard FHIR ``{"resourceType": ..., "id": ...}``
    # representation. ``uuid`` is the FHIR id (what GET ``Patient/<uuid>``
    # uses); ``pid`` is OpenEMR's internal numeric id and isn't useful for
    # FHIR refs. Fall through to ``id`` for any future spec-compliant
    # response shape.
    rid = None
    if isinstance(body, dict):
        rid = body.get("uuid") or body.get("id")
    if not rid:
        raise SeedError(
            f"POST {resource_type} returned no id; status={response.status_code} "
            f"body={response.text[:800]!r}"
        )
    return str(rid)


# ---------- orchestration ----------


async def seed_one_patient(
    http: httpx.AsyncClient,
    *,
    fhir_base_url: str,
    token: str,
    patient_data: dict[str, object],
) -> str:
    patient_body = build_patient(
        patient_data["demographics"]  # type: ignore[arg-type]
    )
    patient_id = await _post_resource(
        http,
        fhir_base_url=fhir_base_url,
        token=token,
        resource=patient_body,
    )
    patient_ref = f"Patient/{patient_id}"

    builders = [
        ("problems", build_condition),
        ("meds", build_medication_request),
        ("allergies", build_allergy),
        ("labs", build_observation),
        ("visits", build_encounter),
        ("notes", build_document_reference),
    ]
    for key, builder in builders:
        items = patient_data.get(key, []) or []
        if not isinstance(items, list):
            continue
        for item in items:
            resource = builder(patient_ref, item)
            await _post_resource(
                http,
                fhir_base_url=fhir_base_url,
                token=token,
                resource=resource,
            )

    return patient_id


async def main_async(args: argparse.Namespace) -> int:
    fixture = json.loads(FIXTURE_PATH.read_text())
    patients_block = fixture.get("patients", {})
    if not isinstance(patients_block, dict):
        print("fixture missing 'patients' block", file=sys.stderr)
        return 2

    private_key_pem = Path(args.private_key).read_bytes()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        oauth = OAuthClient(
            token_url=args.token_url,
            client_id=args.client_id,
            private_key_pem=private_key_pem,
            key_id=args.key_id,
            http_client=http,
            scopes=WRITE_SCOPES,
        )
        token = await oauth.get_access_token()
        print(f"Got writer token (length {len(token)}).", file=sys.stderr)

        seeded: dict[str, str] = {}
        for fixture_id, patient_data in patients_block.items():
            if not isinstance(patient_data, dict):
                continue
            name = str(
                patient_data.get("demographics", {}).get(  # type: ignore[union-attr]
                    "name", f"fixture-{fixture_id}"
                )
            )
            print(f"Seeding {name} (fixture id {fixture_id}) ...", file=sys.stderr)
            try:
                assigned = await seed_one_patient(
                    http,
                    fhir_base_url=args.fhir_base_url,
                    token=token,
                    patient_data=patient_data,
                )
            except SeedError as exc:
                print(f"  FAILED: {exc}", file=sys.stderr)
                return 1
            print(f"  -> Patient/{assigned}", file=sys.stderr)
            seeded[name] = assigned

    args.out.write_text(json.dumps(seeded, indent=2))
    print(f"\nSeeded {len(seeded)} patients. Map saved to {args.out}.", file=sys.stderr)
    print(
        "Pick any value as OPENEMR_TEST_PATIENT_ID for the integration tests.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fhir-base-url", required=True)
    parser.add_argument("--token-url", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument(
        "--private-key",
        type=Path,
        required=True,
        help="Path to the PKCS8 PEM private key for the writer client.",
    )
    parser.add_argument("--key-id", required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("seeded-patient-ids.json"),
        help=(
            "Where to write the {patient_name: assigned_uuid} map "
            "(default: ./seeded-patient-ids.json)."
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
