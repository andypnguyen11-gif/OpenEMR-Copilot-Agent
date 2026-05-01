"""OAuth2 client for the agent service → OpenEMR FHIR boundary.

Second of the two trust layers (ARCHITECTURE §4): the JWT verified in
``jwt_verifier.py`` carries per-clinician identity from PHP→agent; this OAuth
token carries agent-service identity from agent→OpenEMR. Per-clinician RBAC
lives at the tool layer (PR 7) against the JWT claims, not at OAuth — the
OAuth token is intentionally coarse, scoped to the system-level read surface
the agent ever needs.

Uses the SMART Backend Services scope namespace (``system/*``) over the
``client_credentials`` grant with the JWT-bearer ``client_assertion`` form of
client authentication (RFC 7523 §2.2). OpenEMR's confidential-client OAuth2
endpoint hard-rejects any registration with ``system/*`` scopes that lacks a
registered JWK (``src/RestControllers/AuthorizationController.php`` lines
312-317), so static-secret authentication is not an option against a real
instance. The minted assertion is RS384-signed with a key whose ``kid``
matches the registered JWK; minting itself lives in
:mod:`clinical_copilot.auth.client_assertion`.

Failure mode is uniform: any non-2xx response, malformed body, missing
field, or unexpected ``token_type`` raises :class:`OAuthError`. The error
message is a server-side diagnostic and must never reach the user — callers
translate to a generic 503.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ValidationError

from clinical_copilot.auth.client_assertion import ClientAssertionMinter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


SCOPES: tuple[str, ...] = (
    "system/Patient.read",
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
)

# Refresh this many seconds before exp to prevent a token from going stale
# mid-request. 60s on a ~1-hour token is generous; tightening trades cache
# lifetime for clock-skew safety. The same constant gates the cache-hit check
# and the in-lock re-check, so they can't disagree.
REFRESH_LEEWAY = timedelta(seconds=60)

HTTP_BAD_REQUEST = 400

# RFC 7523 §2.2 — fixed string the OAuth server inspects to recognize that
# the request body carries a JWT-bearer client_assertion rather than a
# static secret.
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class OAuthError(RuntimeError):
    """Any failure on the OAuth boundary — never surfaced to user output.

    Carries a server-side diagnostic so logs can pinpoint which check
    failed (transport / status / body / token_type). Treat the message as
    sensitive: it may quote bytes from the OpenEMR response.
    """


class _TokenResponse(BaseModel):
    """Subset of RFC 6749 §5.1 token response we care about.

    Extra fields (``refresh_token``, ``scope``, ...) are tolerated and
    discarded — we don't use refresh_token in client_credentials, and the
    scope echo is informational only.
    """

    access_token: str
    token_type: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class _CachedToken:
    access_token: str
    expires_at: datetime


class OAuthClient:
    """Async OAuth2 client_credentials client with single-flight token cache.

    The lock collapses concurrent ``get_access_token`` calls onto one network
    fetch — without it, N concurrent requests on a cold cache fire N parallel
    token requests at OpenEMR. The double-check pattern (cache read outside
    the lock, then again inside) keeps the lock off the hot path once a
    token is cached.

    The HTTP client is injected so callers manage its lifecycle; this lets
    tests use ``httpx.MockTransport`` and lets production share a single
    long-lived ``httpx.AsyncClient`` across boundaries (FHIR, OAuth, ...).
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        private_key_pem: bytes,
        key_id: str,
        http_client: httpx.AsyncClient,
        scopes: Sequence[str] = SCOPES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not token_url:
            raise ValueError("token_url must be non-empty")
        if not client_id:
            raise ValueError("client_id must be non-empty")
        if not scopes:
            raise ValueError("scopes must be non-empty")
        self._token_url = token_url
        self._client_id = client_id
        self._scope = " ".join(scopes)
        self._http = http_client
        self._clock = clock or _utcnow
        self._lock = asyncio.Lock()
        self._cached: _CachedToken | None = None
        # Minter validates private_key_pem and key_id at construction —
        # raises ValueError on empty/invalid input, so misconfiguration
        # fails at OAuthClient init rather than at the first token fetch.
        self._assertion_minter = ClientAssertionMinter(
            private_key_pem=private_key_pem,
            key_id=key_id,
            issuer=client_id,
            audience=token_url,
            clock=self._clock,
        )

    async def get_access_token(self) -> str:
        """Return a valid bearer token, fetching or refreshing as needed.

        Cached read happens without the lock so the steady-state cost is
        a single attribute read. The lock is taken only when a refresh
        looks necessary; inside the lock we re-check in case another
        coroutine refreshed while we were waiting.
        """

        cached = self._cached
        if cached is not None and self._still_fresh(cached):
            return cached.access_token

        async with self._lock:
            cached = self._cached
            if cached is not None and self._still_fresh(cached):
                return cached.access_token
            self._cached = await self._fetch_token()
            return self._cached.access_token

    def _still_fresh(self, cached: _CachedToken) -> bool:
        return self._clock() + REFRESH_LEEWAY < cached.expires_at

    async def _fetch_token(self) -> _CachedToken:
        # Capture the clock *before* the network call: the response's
        # ``expires_in`` is relative to issue time, not arrival time, and
        # we want a conservative expiry that doesn't include round-trip
        # latency.
        request_started = self._clock()
        # Mint a fresh assertion per fetch — the per-call ``jti`` is the
        # replay defense, so reusing one across fetches would defeat it.
        assertion = self._assertion_minter.mint()
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": CLIENT_ASSERTION_TYPE,
                    "client_assertion": assertion,
                    "scope": self._scope,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"token request transport failure: {exc}") from exc

        if response.status_code >= HTTP_BAD_REQUEST:
            # Body is truncated to bound log noise; the full response is
            # still available on the underlying httpx exception chain
            # for ad-hoc debugging.
            raise OAuthError(
                f"token request failed: status={response.status_code} body={response.text[:200]!r}"
            )

        try:
            body = _TokenResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise OAuthError(f"malformed token response: {exc}") from exc

        if body.token_type.lower() != "bearer":
            raise OAuthError(f"unexpected token_type: {body.token_type!r}")

        if body.expires_in <= 0:
            raise OAuthError(f"non-positive expires_in: {body.expires_in}")

        expires_at = request_started + timedelta(seconds=body.expires_in)
        return _CachedToken(access_token=body.access_token, expires_at=expires_at)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
