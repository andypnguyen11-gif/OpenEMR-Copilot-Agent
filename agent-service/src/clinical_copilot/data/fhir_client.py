"""Async FHIR R4 client for OpenEMR.

The third trust layer (after the PR 4 HMAC JWT and the PR 5.5 OAuth boundary)
is the bearer-attached read against ``${FHIR_BASE_URL}``. Every method here
fetches a fresh access token from the injected :class:`OAuthClient` and
attaches it as ``Authorization: Bearer <token>`` — there is no path that
fetches FHIR data without an OAuth round-trip first.

Failure modes:

- 4xx responses raise :class:`FhirError` immediately. They are not transient
  (auth misconfiguration, bad search params, missing resource); retrying
  hides the bug and adds latency.
- 5xx responses and transport errors retry **once** after a small fixed
  backoff. The bound is intentionally tight: PR 25 owns the full timeout /
  cold-start / circuit-breaker story, and a noisy retry loop here would mask
  the problem PR 25 is supposed to surface.
- Malformed JSON or missing required fields raise :class:`FhirError` via
  Pydantic ``ValidationError`` chaining. Bad parses are silent killers — a
  half-decoded resource that the tool layer projects as ``status=None`` is
  worse than a thrown error, because it would surface to clinicians as if
  the data were missing rather than corrupted.

Pagination is **not** followed. OpenEMR returns at most ``_count`` entries
(default 100); patient-scoped queries virtually never exceed that for the
resources the agent reads. PR 13 revisits this when the discrepancy engine
needs lab history past 100 rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import httpx
from pydantic import ValidationError

from clinical_copilot.auth.oauth_client import OAuthClient, OAuthError
from clinical_copilot.data.models import (
    AllergyIntolerance,
    Bundle,
    Condition,
    DocumentReference,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
)

HTTP_BAD_REQUEST = 400
HTTP_INTERNAL_SERVER_ERROR = 500

# Two attempts total: the first try and a single retry. Named so the
# magic-value lint doesn't trip and so a future "bump retry count"
# refactor lands in one place instead of two `== 2` comparisons.
MAX_ATTEMPTS = 2

# One retry on 5xx / transport — see module docstring for why this is
# tight. The delay is short because PR 25's higher-level retry/timeout
# wrapper expects to be the long-haul reliability layer; this one only
# absorbs single-frame transport blips.
RETRY_BACKOFF = timedelta(milliseconds=200)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Lab-category Observation filter. OpenEMR honours ``category=laboratory``
# (case-sensitive) on FHIR R4; broadening this would mix in vitals and
# social history, which the lab tool isn't designed to surface.
LAB_CATEGORY = "laboratory"


_ResourceT = TypeVar(
    "_ResourceT",
    Patient,
    Condition,
    MedicationRequest,
    AllergyIntolerance,
    Observation,
    Encounter,
    DocumentReference,
)


class FhirError(RuntimeError):
    """Any failure on the FHIR boundary — never surfaced to user output.

    Carries a server-side diagnostic so logs can pinpoint which check
    failed (transport / status / parse). Treat the message as sensitive:
    it may quote bytes from the FHIR response, including resource ids
    that would be PHI in production.
    """


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class FhirClient:
    """Async FHIR R4 client with OAuth bearer auth and bounded retry.

    The HTTP client is injected so callers manage its lifecycle and tests
    can use ``httpx.MockTransport``. The OAuth client is also injected
    so every request gets a fresh-or-cached token; the FhirClient itself
    never reads OAuth env vars or files.

    Construction is intentionally pure: all boundary state (network, time,
    auth) goes in via the constructor so a misconfigured deployment fails
    at app boot rather than at the first FHIR call.
    """

    def __init__(
        self,
        *,
        base_url: str,
        oauth: OAuthClient,
        http_client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
        retry_backoff: timedelta = RETRY_BACKOFF,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be non-empty")
        if retry_backoff < timedelta(0):
            raise ValueError("retry_backoff must be non-negative")
        # Strip trailing slash so resource paths can join with a single
        # leading slash without producing ``//Patient``-style URLs that
        # some servers normalise and others don't.
        self._base_url = base_url.rstrip("/")
        self._oauth = oauth
        self._http = http_client
        self._clock = clock or _utcnow
        self._retry_backoff = retry_backoff

    # ---------- public resource methods ----------

    async def get_patient(self, patient_id: str) -> Patient:
        """Fetch a single Patient by id.

        Used by the orchestrator to verify the JWT's ``patient_id`` claim
        actually exists before launching tool calls — a malformed token
        with a fabricated id would otherwise surface as an empty result
        from every search, which looks like ``patient has no records``
        rather than ``something is wrong with auth``.
        """
        if not patient_id:
            raise ValueError("patient_id must be non-empty")
        body = await self._get(f"/Patient/{patient_id}")
        return self._parse_resource(body, Patient)

    async def search_conditions(self, *, patient_id: str) -> list[Condition]:
        return await self._search("Condition", {"patient": patient_id}, Condition)

    async def search_medications(self, *, patient_id: str) -> list[MedicationRequest]:
        return await self._search("MedicationRequest", {"patient": patient_id}, MedicationRequest)

    async def search_allergies(self, *, patient_id: str) -> list[AllergyIntolerance]:
        return await self._search("AllergyIntolerance", {"patient": patient_id}, AllergyIntolerance)

    async def search_lab_observations(self, *, patient_id: str) -> list[Observation]:
        return await self._search(
            "Observation",
            {"patient": patient_id, "category": LAB_CATEGORY},
            Observation,
        )

    async def search_encounters(self, *, patient_id: str) -> list[Encounter]:
        return await self._search("Encounter", {"patient": patient_id}, Encounter)

    async def search_document_references(self, *, patient_id: str) -> list[DocumentReference]:
        return await self._search("DocumentReference", {"patient": patient_id}, DocumentReference)

    # ---------- internals ----------

    async def _search(
        self,
        resource_type: str,
        params: Mapping[str, str],
        model: type[_ResourceT],
    ) -> list[_ResourceT]:
        if not params.get("patient"):
            raise ValueError("FHIR searches must be scoped by patient")
        body = await self._get(f"/{resource_type}", params=dict(params))
        return self._parse_bundle(body, resource_type, model)

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> object:
        url = self._base_url + path
        try:
            token = await self._oauth.get_access_token()
        except OAuthError as exc:
            # Wrap so callers only need one ``except`` — every failure on
            # the FHIR path comes through FhirError. The original cause
            # chain via ``__cause__`` keeps the diagnostic.
            raise FhirError(f"OAuth token unavailable for {path}: {exc}") from exc

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        }

        response = await self._send_with_retry(url=url, params=dict(params or {}), headers=headers)

        try:
            return response.json()
        except ValueError as exc:
            raise FhirError(f"malformed JSON from {path}: {response.text[:200]!r}") from exc

    async def _send_with_retry(
        self,
        *,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response:
        """One retry on 5xx / transport, never on 4xx.

        The retry budget is intentionally tiny (see module docstring); a
        sleep-loop here would tile over PR 25's timeout / circuit-breaker
        layer and make latency regressions invisible to the metrics there.
        """
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    raise FhirError(f"FHIR transport failure after retry: {exc}") from exc
                await asyncio.sleep(self._retry_backoff.total_seconds())
                continue

            if response.status_code < HTTP_BAD_REQUEST:
                return response

            if response.status_code < HTTP_INTERNAL_SERVER_ERROR:
                # 4xx — no retry, surface immediately. 401 is the most
                # common case in practice and means the bearer was
                # rejected (e.g. registration disabled mid-flight); we
                # want that to fail loud, not retry-then-fail-loud.
                raise FhirError(
                    f"FHIR client error: status={response.status_code} "
                    f"url={url} body={response.text[:200]!r}"
                )

            # 5xx — retry once, then surface.
            if attempt == MAX_ATTEMPTS:
                raise FhirError(
                    f"FHIR server error after retry: status={response.status_code} "
                    f"url={url} body={response.text[:200]!r}"
                )
            await asyncio.sleep(self._retry_backoff.total_seconds())

        # Unreachable: the loop body either returns or raises on every
        # iteration. The fallback raise keeps mypy honest about the
        # function's claim to always produce a Response.
        raise FhirError(f"FHIR retry loop exited without a response: last={last_exc!r}")

    def _parse_resource(self, body: object, model: type[_ResourceT]) -> _ResourceT:
        if not isinstance(body, dict):
            raise FhirError(f"expected a FHIR resource object, got {type(body).__name__}")
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise FhirError(f"failed to parse {model.__name__}: {exc}") from exc

    def _parse_bundle(
        self,
        body: object,
        resource_type: str,
        model: type[_ResourceT],
    ) -> list[_ResourceT]:
        if not isinstance(body, dict):
            raise FhirError(f"expected a FHIR Bundle object, got {type(body).__name__}")
        try:
            bundle = Bundle.model_validate(body)
        except ValidationError as exc:
            raise FhirError(f"failed to parse Bundle: {exc}") from exc

        if bundle.resource_type and bundle.resource_type != "Bundle":
            raise FhirError(f"expected Bundle, got resourceType={bundle.resource_type!r}")

        out: list[_ResourceT] = []
        for entry in bundle.entry:
            resource = entry.resource
            if resource is None:
                continue
            # Defensive narrow: a Bundle can in principle mix resource
            # types (OperationOutcome entries, contained errors). Skip
            # anything that isn't the requested type rather than failing
            # the whole search — one stray OperationOutcome shouldn't
            # nuke a successful query for the rest.
            if isinstance(resource, dict):
                rt = resource.get("resourceType")
                if rt and rt != resource_type:
                    continue
            try:
                out.append(model.model_validate(resource))
            except ValidationError as exc:
                raise FhirError(f"failed to parse {model.__name__} entry: {exc}") from exc
        return out
