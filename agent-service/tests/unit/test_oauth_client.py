"""Unit tests for the OAuth2 JWT-bearer client_assertion boundary client.

Mocks OpenEMR's token endpoint via :class:`httpx.MockTransport` so the suite
runs offline. The contract under test is the one PR 6's FHIR client will
depend on:

- A valid token is reused across calls until the freshness window closes.
- Concurrent callers share a single network fetch (single-flight).
- Every failure mode collapses into :class:`OAuthError` with a server-side
  diagnostic — the message is never surfaced to the user.
- Form encoding matches RFC 7523 §2.2 (``grant_type=client_credentials`` +
  ``client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer``
  + ``client_assertion=<JWT>`` + ``scope`` as a space-delimited list) so
  OpenEMR's confidential-client flow accepts the request.
- Each posted ``client_assertion`` is a fresh RS384 JWT with the registered
  ``kid``; the assertion's claims (iss/sub/aud) match the OAuth client's
  configured identity and target.

The integration counterpart that hits a real OpenEMR is in
``tests/integration/test_oauth_client.py`` and is gated by
``OPENEMR_INTEGRATION=1``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from clinical_copilot.auth.oauth_client import (
    CLIENT_ASSERTION_TYPE,
    REFRESH_LEEWAY,
    SCOPES,
    OAuthClient,
    OAuthError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOKEN_URL = "https://openemr.example.test/oauth2/default/token"
CLIENT_ID = "agent-service-client"
KEY_ID = "agent-service-key-2026"


class _MockTokenServer:
    """Records every request and serves a queue of canned responses.

    Each test queues exactly the responses it expects and then asserts
    against ``calls`` afterward. An empty queue is a hard error so a
    silent over-fetch (e.g. broken cache) shows up as a failed test rather
    than as a falsy result.
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._responses: list[httpx.Response] = []

    def queue(self, response: httpx.Response) -> None:
        self._responses.append(response)

    def queue_ok(
        self,
        *,
        access_token: str = "tok-abc",
        token_type: str = "Bearer",
        expires_in: int = 3600,
    ) -> None:
        self.queue(
            httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "token_type": token_type,
                    "expires_in": expires_in,
                },
            )
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected request to {request.url}: queue is empty")
        return self._responses.pop(0)


class _Clock:
    """Mutable clock for deterministic exp / refresh tests.

    Production injects ``None`` and gets wall time; here we want to step
    time forward inside a single test without sleeping.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def __call__(self) -> datetime:
        return self._now


@pytest.fixture
def server() -> _MockTokenServer:
    return _MockTokenServer()


@pytest.fixture
async def http_client(server: _MockTokenServer) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(server)
    async with httpx.AsyncClient(transport=transport) as client:
        yield client


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC))


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    """One RSA-2048 keypair for the whole module — generation is the slow step."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def private_pem(keypair: tuple[bytes, bytes]) -> bytes:
    return keypair[0]


@pytest.fixture
def public_pem(keypair: tuple[bytes, bytes]) -> bytes:
    return keypair[1]


@pytest.fixture
def oauth(
    http_client: httpx.AsyncClient,
    clock: _Clock,
    private_pem: bytes,
) -> OAuthClient:
    return OAuthClient(
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        private_key_pem=private_pem,
        key_id=KEY_ID,
        http_client=http_client,
        clock=clock,
    )


def _form_field(request: httpx.Request, name: str) -> str:
    """Pull a single form field from a posted application/x-www-form-urlencoded body.

    ``parse_qs`` returns ``list[str]`` per key; we expect exactly one value
    for every field we send. Asserting that here surfaces an unexpected
    duplicate (which would smell like a body-encoding bug) instead of
    silently picking the first.
    """
    body = parse_qs(request.content.decode())
    values = body.get(name, [])
    assert len(values) == 1, f"expected exactly one {name!r} value, got {values!r}"
    return values[0]


# ---------- happy path & form encoding ----------


async def test_first_call_fetches_token_and_returns_access_token(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    server.queue_ok(access_token="first-token")

    token = await oauth.get_access_token()

    assert token == "first-token"
    assert len(server.calls) == 1


async def test_post_body_carries_jwt_bearer_assertion(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """RFC 7523 §2.2 form-encoding pinned so OpenEMR accepts the request.

    A regression that swapped back to ``client_secret``, dropped the
    ``client_assertion_type``, sent the JWT in a header instead of the
    body, or omitted the scope parameter would only surface in
    integration. Pin every field here so unit coverage catches it.
    """

    server.queue_ok()

    await oauth.get_access_token()

    request = server.calls[0]
    assert _form_field(request, "grant_type") == "client_credentials"
    assert _form_field(request, "client_assertion_type") == CLIENT_ASSERTION_TYPE
    # client_assertion is the minted JWT — three base64url segments separated by '.'
    assertion = _form_field(request, "client_assertion")
    assert assertion.count(".") == 2
    # Static-secret flow must be gone; OpenEMR rejects the registration
    # that would accept it, and leaving the field in would leak the
    # secret-equivalent material into logs at no benefit.
    body = parse_qs(request.content.decode())
    assert "client_secret" not in body
    scope = _form_field(request, "scope")
    for s in SCOPES:
        assert s in scope


async def test_assertion_is_rs384_with_configured_kid_and_claims(
    server: _MockTokenServer, oauth: OAuthClient, public_pem: bytes
) -> None:
    """The minted JWT must verify under the registered public key with the
    exact alg/kid/claims OpenEMR's signer enforces.

    Decoded with the public PEM here so a regression that quietly
    switched algorithm (e.g. HS256), forgot the ``kid``, or pointed
    ``aud`` at the wrong endpoint is caught at unit level instead of
    blowing up against a real OpenEMR.
    """

    server.queue_ok()

    await oauth.get_access_token()

    assertion = _form_field(server.calls[0], "client_assertion")
    header = pyjwt.get_unverified_header(assertion)
    assert header["alg"] == "RS384"
    assert header["kid"] == KEY_ID

    payload = pyjwt.decode(
        assertion,
        public_pem,
        algorithms=["RS384"],
        audience=TOKEN_URL,
        options={"verify_exp": False},
    )
    assert payload["iss"] == CLIENT_ID
    assert payload["sub"] == CLIENT_ID
    assert payload["aud"] == TOKEN_URL
    assert isinstance(payload["jti"], str) and payload["jti"]


async def test_each_fetch_mints_a_fresh_assertion(
    server: _MockTokenServer, oauth: OAuthClient, clock: _Clock
) -> None:
    """Per-call ``jti`` is the replay defense — reusing one across fetches
    would defeat it. Two refreshes must post two distinct assertions.
    """

    server.queue_ok(access_token="t1", expires_in=60)
    server.queue_ok(access_token="t2", expires_in=60)

    await oauth.get_access_token()
    clock.advance(timedelta(seconds=120))
    await oauth.get_access_token()

    a1 = _form_field(server.calls[0], "client_assertion")
    a2 = _form_field(server.calls[1], "client_assertion")
    assert a1 != a2


async def test_request_targets_configured_token_url(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    server.queue_ok()

    await oauth.get_access_token()

    assert str(server.calls[0].url) == TOKEN_URL


# ---------- caching & refresh ----------


async def test_second_call_within_freshness_uses_cache(
    server: _MockTokenServer, oauth: OAuthClient, clock: _Clock
) -> None:
    """Cache hit means no second network call and no second response needed."""

    server.queue_ok(access_token="cached", expires_in=3600)

    first = await oauth.get_access_token()
    clock.advance(timedelta(seconds=60))  # well within freshness
    second = await oauth.get_access_token()

    assert first == second == "cached"
    assert len(server.calls) == 1


async def test_call_within_refresh_leeway_triggers_refresh(
    server: _MockTokenServer, oauth: OAuthClient, clock: _Clock
) -> None:
    """Token within leeway of exp must refresh, even if technically still valid.

    The leeway exists so a token doesn't go stale mid-request; if it
    didn't trigger a refresh, a request started right before exp would
    arrive at OpenEMR with an expired token.
    """

    server.queue_ok(access_token="t1", expires_in=3600)
    server.queue_ok(access_token="t2", expires_in=3600)

    first = await oauth.get_access_token()
    clock.advance(timedelta(seconds=3600) - REFRESH_LEEWAY + timedelta(seconds=1))
    second = await oauth.get_access_token()

    assert first == "t1"
    assert second == "t2"
    assert len(server.calls) == 2


async def test_call_after_exp_triggers_refresh(
    server: _MockTokenServer, oauth: OAuthClient, clock: _Clock
) -> None:
    server.queue_ok(access_token="t1", expires_in=60)
    server.queue_ok(access_token="t2", expires_in=60)

    first = await oauth.get_access_token()
    clock.advance(timedelta(seconds=120))
    second = await oauth.get_access_token()

    assert first == "t1"
    assert second == "t2"
    assert len(server.calls) == 2


async def test_concurrent_callers_share_a_single_fetch(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """N coroutines hitting an empty cache must collapse onto one network call.

    Without the lock + double-check, every concurrent caller would race
    their own fetch and DDoS the OpenEMR token endpoint on cold start.
    """

    server.queue_ok(access_token="single-flight-token")

    tokens = await asyncio.gather(*(oauth.get_access_token() for _ in range(8)))

    assert tokens == ["single-flight-token"] * 8
    assert len(server.calls) == 1


# ---------- error paths ----------


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 500, 502, 503],
)
async def test_non_2xx_response_raises_oauth_error(
    server: _MockTokenServer, oauth: OAuthClient, status_code: int
) -> None:
    server.queue(httpx.Response(status_code, text="rejected"))

    with pytest.raises(OAuthError) as excinfo:
        await oauth.get_access_token()

    assert str(status_code) in str(excinfo.value)


async def test_malformed_body_raises_oauth_error(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    server.queue(httpx.Response(200, text="not-json"))

    with pytest.raises(OAuthError) as excinfo:
        await oauth.get_access_token()

    assert "malformed" in str(excinfo.value).lower()


async def test_missing_required_field_raises_oauth_error(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    server.queue(
        httpx.Response(
            200,
            json={"access_token": "x", "token_type": "Bearer"},  # no expires_in
        )
    )

    with pytest.raises(OAuthError):
        await oauth.get_access_token()


async def test_non_bearer_token_type_is_rejected(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """Anything other than ``Bearer`` (case-insensitive) is a contract break.

    OpenEMR is documented to issue Bearer tokens; if it ever returned
    ``MAC`` or some custom scheme, our downstream FHIR client wouldn't
    know how to use it. Fail loud rather than silently mis-attaching.
    """

    server.queue(
        httpx.Response(
            200,
            json={"access_token": "x", "token_type": "MAC", "expires_in": 3600},
        )
    )

    with pytest.raises(OAuthError) as excinfo:
        await oauth.get_access_token()

    assert "token_type" in str(excinfo.value)


async def test_bearer_token_type_is_case_insensitive(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """RFC 6749 §5.1 says token_type is case-insensitive ('Bearer' / 'bearer').

    OpenEMR uses ``Bearer`` but the spec allows lowercase, so the
    comparison is normalized. Pin the behavior so a future "tighten the
    check" refactor doesn't accidentally regress it.
    """

    server.queue(
        httpx.Response(
            200,
            json={"access_token": "x", "token_type": "bearer", "expires_in": 3600},
        )
    )

    token = await oauth.get_access_token()
    assert token == "x"


async def test_non_positive_expires_in_raises_oauth_error(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """A token born expired (or in the past) is unusable.

    Almost certainly a server bug or a tampered response, but the cost
    of caching it would be a hot-loop refresh storm — fail closed.
    """

    server.queue(
        httpx.Response(
            200,
            json={"access_token": "x", "token_type": "Bearer", "expires_in": 0},
        )
    )

    with pytest.raises(OAuthError):
        await oauth.get_access_token()


async def test_transport_error_is_translated_to_oauth_error(
    clock: _Clock,
    private_pem: bytes,
) -> None:
    """A network-layer failure must surface as OAuthError, not raw httpx.

    Callers (the FHIR client in PR 6, retries in PR 25) only catch
    OAuthError. Letting httpx exceptions leak would bypass that handler.

    Builds its own client because the shared ``http_client`` fixture
    routes through the queue-asserting ``_MockTokenServer``, and this
    test wants a transport that raises before any response.
    """

    def _explode(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(_explode)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            private_key_pem=private_pem,
            key_id=KEY_ID,
            http_client=http,
            clock=clock,
        )

        with pytest.raises(OAuthError) as excinfo:
            await client.get_access_token()

    assert "transport" in str(excinfo.value).lower()


# ---------- constructor validation ----------
#
# These tests instantiate OAuthClient and never call get_access_token, so the
# injected http_client is never used — but it still needs proper async
# teardown to keep ``filterwarnings = ["error"]`` from promoting an
# unclosed-client warning into a failure. Hence the async fixture.


async def test_constructor_rejects_empty_token_url(
    http_client: httpx.AsyncClient, private_pem: bytes
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url="",
            client_id=CLIENT_ID,
            private_key_pem=private_pem,
            key_id=KEY_ID,
            http_client=http_client,
        )


async def test_constructor_rejects_empty_client_id(
    http_client: httpx.AsyncClient, private_pem: bytes
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id="",
            private_key_pem=private_pem,
            key_id=KEY_ID,
            http_client=http_client,
        )


async def test_constructor_rejects_empty_private_key(
    http_client: httpx.AsyncClient,
) -> None:
    """An empty ``private_key_pem`` would mean no signer is available; the
    minter raises at OAuthClient init so the failure is loud and early.
    """
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            private_key_pem=b"",
            key_id=KEY_ID,
            http_client=http_client,
        )


async def test_constructor_rejects_empty_key_id(
    http_client: httpx.AsyncClient, private_pem: bytes
) -> None:
    """``kid`` is what OpenEMR uses to resolve the registered JWK; without
    it, no token fetch could ever succeed.
    """
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            private_key_pem=private_pem,
            key_id="",
            http_client=http_client,
        )


async def test_constructor_rejects_empty_scopes(
    http_client: httpx.AsyncClient, private_pem: bytes
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            private_key_pem=private_pem,
            key_id=KEY_ID,
            http_client=http_client,
            scopes=(),
        )


def test_default_scopes_match_audited_set() -> None:
    """The PR 5 sub-task list froze the system/* read scopes; pin them.

    Adding a scope is a security-impacting change (broadens the
    agent-service's read surface) and should require an explicit code
    diff to this list, not a silent drift. ``system/MedicationStatement.read``
    was dropped during PR 5.5 registration: OpenEMR's FHIR surface rejects
    it as ``invalid_scope`` (``MedicationRequest`` covers the use case).
    """

    assert SCOPES == (
        "system/Patient.read",
        "system/Condition.read",
        "system/MedicationRequest.read",
        "system/AllergyIntolerance.read",
        "system/Observation.read",
        "system/Encounter.read",
        "system/DocumentReference.read",
    )
