"""Bucket imported patients into eval-friendly groups by chart shape.

Run after ``devtools import-random-patients`` lands a fresh batch of synthetic
charts. The eval harness reads the output file (``eval-patient-ids.json``)
instead of hardcoding pids in case JSON, so a re-import doesn't cascade into
test edits.

Buckets:

- ``full_chart``    — has conditions + meds + allergies + encounters
                      (happy-path target)
- ``no_allergies``  — has conditions + meds + encounters but no allergies
                      (missing-data: ``what allergies does this patient have?``
                      should produce a clean NO_DATA, not a fabrication)
- ``no_problems``   — has meds but no medical-problem entries
                      (missing-data + sanity check that meds-without-problems
                      doesn't silently invent a diagnosis)
- ``default``       — any patient (ambiguous-query suite uses these as the
                      query target since the patient shape isn't the variable)

Doubles as the FHIR-projection sanity check called out in TASKS.md PR 22 —
if a CCDA-imported chart shows non-zero rows in MySQL but the FHIR search
returns empty, that's an environmental gap in OpenEMR's FHIR projection,
not an agent bug. The script logs the discrepancy when it sees one so the
eval-case author can decide whether the gap is the eval target or a thing
to work around.

Usage::

    uv run python scripts/snapshot_eval_patients.py \\
        --openemr-url http://localhost:8300 \\
        --client-id "$(jq -r .client_id oauth-registration-local.json)" \\
        --private-key agent-service-private-key.pem \\
        --key-id agent-service-2026-04 \\
        --out eval-patient-ids.json

Output is gitignored — check the JSON against the snapshot the eval harness
expects (re-runs produce a new file with new pids; that's the design).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinical_copilot.auth.oauth_client import OAuthClient  # noqa: E402

READ_SCOPES: tuple[str, ...] = (
    "system/Patient.read",
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
)

# Cap on patients pulled per page. OpenEMR's FHIR layer paginates, but for
# bucket-style triage we just need the universe; ``_count=200`` is well above
# a typical ``import-random-patients`` batch and below what would matter for
# memory.
PAGE_SIZE = 200


async def _bearer_get(
    http: httpx.AsyncClient,
    *,
    fhir_base: str,
    token: str,
    path: str,
    params: dict[str, str] | None = None,
) -> dict:
    response = await http.get(
        f"{fhir_base.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        },
        params=params or {},
    )
    response.raise_for_status()
    return response.json()


async def _list_patients(
    http: httpx.AsyncClient, *, fhir_base: str, token: str
) -> list[dict]:
    bundle = await _bearer_get(
        http,
        fhir_base=fhir_base,
        token=token,
        path="/Patient",
        params={"_count": str(PAGE_SIZE)},
    )
    return [
        entry["resource"]
        for entry in (bundle.get("entry") or [])
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]


async def _count_for_patient(
    http: httpx.AsyncClient,
    *,
    fhir_base: str,
    token: str,
    resource_type: str,
    patient_id: str,
    extra_params: dict[str, str] | None = None,
) -> int:
    """Count entries for a resource scoped to one patient.

    OpenEMR's FHIR layer reports ``Bundle.total`` as the count of returned
    entries (i.e. echoes ``_count``), not the unconstrained match count, so
    ``_summary=count`` and a low ``_count`` both undercount silently. Pull a
    big enough page to cover any realistic patient and count ``entry``
    directly. ``_count=200`` is well above what Synthea-generated charts hit
    and small enough to keep round-trips snappy.
    """
    params = {"patient": patient_id, "_count": "200"}
    if extra_params:
        params.update(extra_params)
    bundle = await _bearer_get(
        http,
        fhir_base=fhir_base,
        token=token,
        path=f"/{resource_type}",
        params=params,
    )
    return len(bundle.get("entry") or [])


async def _profile_patient(
    http: httpx.AsyncClient,
    *,
    fhir_base: str,
    token: str,
    patient: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    pid = str(patient["id"])
    name = _short_name(patient)
    # Cap in-flight requests for this patient to a small number so the
    # outer semaphore-bound concurrency cleanly translates into total
    # in-flight work (concurrency × 4 ≤ pool size). Without an inner
    # gather, profiling is sequential — the semaphore is what bounds
    # parallelism across patients.
    async with semaphore:
        counts = await asyncio.gather(
            _count_for_patient(
                http, fhir_base=fhir_base, token=token,
                resource_type="Condition", patient_id=pid,
            ),
            _count_for_patient(
                http, fhir_base=fhir_base, token=token,
                resource_type="MedicationRequest", patient_id=pid,
            ),
            _count_for_patient(
                http, fhir_base=fhir_base, token=token,
                resource_type="AllergyIntolerance", patient_id=pid,
            ),
            _count_for_patient(
                http, fhir_base=fhir_base, token=token,
                resource_type="Encounter", patient_id=pid,
            ),
        )
    cond, med, allergy, enc = counts
    return {
        "uuid": pid,
        "name": name,
        "counts": {
            "conditions": cond,
            "medications": med,
            "allergies": allergy,
            "encounters": enc,
        },
    }


def _short_name(patient: dict) -> str:
    names = patient.get("name") or []
    if not names:
        return "(no name)"
    n = names[0]
    given = " ".join(n.get("given") or [])
    family = n.get("family") or ""
    text = f"{given} {family}".strip() or n.get("text") or "(no name)"
    return text


def _bucket(profiles: list[dict]) -> dict[str, list[dict]]:
    """Sort profiles into the four buckets the eval suites consume.

    A patient can appear in multiple buckets; that's intentional — eval
    cases pick by bucket, and a chart that's both ``full_chart`` *and*
    ``no_allergies=False`` is a valid happy-path target. ``default`` always
    contains every profile so the ambiguous suite has the full pool to
    pick from.
    """
    full_chart: list[dict] = []
    no_allergies: list[dict] = []
    no_problems: list[dict] = []
    default: list[dict] = []
    for p in profiles:
        c = p["counts"]
        default.append(p)
        if c["conditions"] > 0 and c["medications"] > 0 and c["encounters"] > 0:
            if c["allergies"] > 0:
                full_chart.append(p)
            else:
                no_allergies.append(p)
        if c["medications"] > 0 and c["conditions"] == 0:
            no_problems.append(p)
    return {
        "full_chart": full_chart,
        "no_allergies": no_allergies,
        "no_problems": no_problems,
        "default": default,
    }


async def main_async(args: argparse.Namespace) -> int:
    private_key_pem = Path(args.private_key).read_bytes()
    fhir_base = args.openemr_url.rstrip("/") + "/apis/default/fhir"
    token_url = args.openemr_url.rstrip("/") + "/oauth2/default/token"

    # ``--insecure`` skips TLS verification — only for local dev against the
    # development-easy stack's self-signed cert. The ``aud`` claim OpenEMR
    # validates against must match its ``site_addr_oath`` global, which for
    # local is ``https://localhost:9300`` — so HTTP via 8300 won't work even
    # though the registration endpoint accepts it.
    # Bound in-flight work via a semaphore (max ``args.concurrency`` patients
    # × 4 searches each) rather than scaling with patient count. The earlier
    # all-at-once gather over ``len(patients)`` saturated the connection pool
    # for any non-trivial import (PoolTimeout at ~100 patients).
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),
        verify=not args.insecure,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    ) as http:
        semaphore = asyncio.Semaphore(args.concurrency)
        oauth = OAuthClient(
            token_url=token_url,
            client_id=args.client_id,
            private_key_pem=private_key_pem,
            key_id=args.key_id,
            http_client=http,
            scopes=READ_SCOPES,
        )
        token = await oauth.get_access_token()
        print(f"Got reader token (length {len(token)}).", file=sys.stderr)

        patients = await _list_patients(http, fhir_base=fhir_base, token=token)
        print(f"Found {len(patients)} patients via FHIR.", file=sys.stderr)
        if not patients:
            print(
                "No patients returned — confirm import-random-patients ran "
                "and the OAuth client has system/Patient.read.",
                file=sys.stderr,
            )
            return 1

        profiles = await asyncio.gather(
            *(
                _profile_patient(
                    http,
                    fhir_base=fhir_base,
                    token=token,
                    patient=p,
                    semaphore=semaphore,
                )
                for p in patients
            )
        )

    buckets = _bucket(profiles)
    out = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "openemr_url": args.openemr_url,
        "patient_count": len(profiles),
        "buckets": buckets,
    }
    args.out.write_text(json.dumps(out, indent=2))

    summary_lines = [f"\nWrote {args.out}."]
    for name, items in buckets.items():
        summary_lines.append(f"  {name}: {len(items)}")
    print("\n".join(summary_lines), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--openemr-url", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval-patient-ids.json"),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help=(
            "Max patients profiled in parallel. Each holds 4 in-flight FHIR "
            "searches; keep concurrency × 4 ≤ pool size (50) to avoid "
            "PoolTimeout."
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "Skip TLS certificate verification. Use against the local "
            "development-easy stack's self-signed cert; never use against "
            "deployed OpenEMR."
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
