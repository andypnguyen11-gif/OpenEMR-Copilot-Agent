"""Unit tests for the OAuth2 client_credentials boundary client.

Mocks OpenEMR's token endpoint via :class:`httpx.MockTransport` so the suite
runs offline. The contract under test is the one PR 6's FHIR client will
depend on:

- A valid token is reused across calls until the freshness window closes.
- Concurrent callers share a single network fetch (single-flight).
- Every failure mode collapses into :class:`OAuthError` with a server-side
  diagnostic — the message is never surfaced to the user.
- Form encoding matches RFC 6749 §4.4 (grant_type, client_id, client_secret,
  scope as a space-delimited list) so OpenEMR's confidential-client flow
  accepts the request.

The integration counterpart that hits a real OpenEMR is in
``tests/integration/test_oauth_client.py`` and is gated by
``OPENEMR_INTEGRATION=1``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from clinical_copilot.auth.oauth_client import (
    REFRESH_LEEWAY,
    SCOPES,
    OAuthClient,
    OAuthError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOKEN_URL = "https://openemr.example.test/oauth2/default/token"
CLIENT_ID = "agent-service-client"
CLIENT_SECRET = "agent-service-secret"


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


@pytest.fixture
def oauth(
    http_client: httpx.AsyncClient,
    clock: _Clock,
) -> OAuthClient:
    return OAuthClient(
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        http_client=http_client,
        clock=clock,
    )


# ---------- happy path & form encoding ----------


async def test_first_call_fetches_token_and_returns_access_token(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    server.queue_ok(access_token="first-token")

    token = await oauth.get_access_token()

    assert token == "first-token"
    assert len(server.calls) == 1


async def test_post_body_contains_client_credentials_and_scope(
    server: _MockTokenServer, oauth: OAuthClient
) -> None:
    """RFC 6749 §4.4 form-encoding pinned so OpenEMR accepts the request.

    A regression that changed the body to JSON, or dropped the scope
    parameter, would only surface in integration. Pin it here so unit
    coverage catches it.
    """

    server.queue_ok()

    await oauth.get_access_token()

    request = server.calls[0]
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert f"client_id={CLIENT_ID}" in body
    assert f"client_secret={CLIENT_SECRET}" in body
    # scope is space-joined per RFC 6749 §3.3, URL-encoded as '+' or '%20'
    assert "scope=" in body
    for s in SCOPES:
        # scope values use '/' which gets URL-encoded; check the bare token
        # name (e.g. "Patient.read") which survives encoding intact.
        assert s.split("/")[1] in body


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
            client_secret=CLIENT_SECRET,
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
    http_client: httpx.AsyncClient,
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url="",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            http_client=http_client,
        )


async def test_constructor_rejects_empty_client_id(
    http_client: httpx.AsyncClient,
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id="",
            client_secret=CLIENT_SECRET,
            http_client=http_client,
        )


async def test_constructor_rejects_empty_client_secret(
    http_client: httpx.AsyncClient,
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            client_secret="",
            http_client=http_client,
        )


async def test_constructor_rejects_empty_scopes(
    http_client: httpx.AsyncClient,
) -> None:
    with pytest.raises(ValueError):
        OAuthClient(
            token_url=TOKEN_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            http_client=http_client,
            scopes=(),
        )


def test_default_scopes_match_audited_set() -> None:
    """The PR 5 sub-task list froze the eight system/* read scopes; pin them.

    Adding a scope is a security-impacting change (broadens the
    agent-service's read surface) and should require an explicit code
    diff to this list, not a silent drift.
    """

    assert SCOPES == (
        "system/Patient.read",
        "system/Condition.read",
        "system/MedicationRequest.read",
        "system/MedicationStatement.read",
        "system/AllergyIntolerance.read",
        "system/Observation.read",
        "system/Encounter.read",
        "system/DocumentReference.read",
    )
