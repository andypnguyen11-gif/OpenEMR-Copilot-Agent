"""Verifier-side tests for the PHP→Python trust boundary token.

The PHP gateway mints HS256 JWTs scoped ``{user_id, role, patient_id, scopes,
nonce}`` and bound to a five-minute window. This file pins the verifier's
contract: a well-formed token round-trips, *every* failure mode is rejected
with a distinct reason, and the FastAPI dependency surfaces those failures
as 401s without leaking detail to the response body.

Mirrors ``tests/Tests/Isolated/Services/Copilot/JwtSignerTest.php`` —
anything that file's signer produces must validate here, and only here.

Tests use real wall-clock time for the verifier (PyJWT's exp check uses
``datetime.now(tz=UTC)`` internally and we don't override that). Tokens
are minted with ``iat`` set near wall time so the 5-minute window is wide
enough to absorb test runtime; tests that want to probe exp/iat boundaries
do so by offsetting ``iat`` explicitly. The replay store is the only piece
that gets a frozen clock — those tests need to advance time deterministically
to verify TTL eviction.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from clinical_copilot.auth.jwt_verifier import (
    InvalidJwtError,
    JwtVerifier,
    require_clinician_claims,
)
from clinical_copilot.auth.session import ClinicianClaims, NonceStore

# 64 bytes — meets the strictest HMAC key-length recommendation (HS512) so
# minting tokens at any HS-family algorithm doesn't trip PyJWT's
# InsecureKeyLengthWarning, which our pytest config promotes to an error.
SECRET = "unit-test-hmac-secret-64-bytes-padded-padded-padded-padded-okayY"


def _fixed_clock(now: datetime) -> Callable[[], datetime]:
    def _clock() -> datetime:
        return now

    return _clock


def _mint(
    *,
    secret: str = SECRET,
    issuer: str = "openemr-gateway",
    audience: str = "clinical-copilot",
    alg: str = "HS256",
    iat: datetime | None = None,
    exp_delta_seconds: int = 300,
    user_id: str = "user-42",
    role: str = "physician",
    patient_id: str = "patient-7",
    scopes: list[str] | None = None,
    nonce: str = "nonce-abc",
    jti: str | None = None,
    extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    """Mint a JWT mimicking what the PHP signer emits.

    Each test parameterizes one axis (alg, secret, exp, missing claim) so
    failures stay attributable to a single divergence from the contract.
    """

    iat_dt = iat or datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": int(iat_dt.timestamp()),
        "exp": int(iat_dt.timestamp()) + exp_delta_seconds,
        "jti": jti or str(uuid.uuid4()),
        "user_id": user_id,
        "role": role,
        "patient_id": patient_id,
        "scopes": scopes if scopes is not None else ["patient/Patient.read"],
        "nonce": nonce,
    }
    for key in drop:
        payload.pop(key, None)
    if extra:
        payload.update(extra)
    return pyjwt.encode(payload, secret, algorithm=alg)


@pytest.fixture
def verifier() -> Iterator[JwtVerifier]:
    """Verifier wired to wall-clock time — PyJWT's exp check uses wall time
    internally, so we don't fight it with a frozen clock here. Each test
    that needs deterministic timing uses ``iat`` offsets from ``now()``."""

    yield JwtVerifier(
        secret=SECRET,
        replay_store=NonceStore(ttl_seconds=300),
    )


def test_well_formed_token_decodes_to_clinician_claims(verifier: JwtVerifier) -> None:
    token = _mint(jti="jti-1")

    claims = verifier.verify(token)

    assert isinstance(claims, ClinicianClaims)
    assert claims.user_id == "user-42"
    assert claims.role == "physician"
    assert claims.patient_id == "patient-7"
    assert claims.scopes == ["patient/Patient.read"]
    assert claims.nonce == "nonce-abc"


def test_tampered_signature_is_rejected(verifier: JwtVerifier) -> None:
    """Forged tokens never validate — that's the whole boundary.

    Flipping the last char of the signature segment is the cheapest
    forgery; if the verifier accepts it, every other signature check is
    broken too.
    """

    token = _mint()
    head, payload, sig = token.split(".")
    # Flip a char in the middle of the signature so the base64-decoded bytes
    # actually change — flipping the last char can land on padding bits and
    # leave the decoded signature unchanged.
    mid = len(sig) // 2
    flipped = "X" if sig[mid] != "X" else "Y"
    tampered_sig = sig[:mid] + flipped + sig[mid + 1 :]
    forged = f"{head}.{payload}.{tampered_sig}"

    with pytest.raises(InvalidJwtError) as excinfo:
        verifier.verify(forged)

    assert "signature" in str(excinfo.value).lower()


def test_token_signed_with_wrong_secret_is_rejected(verifier: JwtVerifier) -> None:
    token = _mint(secret="attacker-secret-pretending-to-be-real")

    with pytest.raises(InvalidJwtError):
        verifier.verify(token)


def test_expired_token_is_rejected(verifier: JwtVerifier) -> None:
    """A token whose exp has passed must fail even if signature is valid.

    Pushing iat back by 10 minutes drops exp 5 minutes into the past,
    which is exactly the leak-window we're guarding against.
    """

    expired_iat = datetime.now(tz=UTC) - timedelta(minutes=10)
    token = _mint(iat=expired_iat)

    with pytest.raises(InvalidJwtError) as excinfo:
        verifier.verify(token)

    assert "expired" in str(excinfo.value).lower()


def test_token_from_the_future_is_rejected(verifier: JwtVerifier) -> None:
    """iat in the future means clock skew or tampering — refuse.

    PyJWT does not enforce future-iat by default; the verifier adds a
    belt-and-suspenders check. A future-dated token is always either a
    misconfiguration or an attack and should not pass.
    """

    future_iat = datetime.now(tz=UTC) + timedelta(minutes=10)
    token = _mint(iat=future_iat)

    with pytest.raises(InvalidJwtError):
        verifier.verify(token)


def test_replayed_jti_is_rejected_within_ttl(verifier: JwtVerifier) -> None:
    """Replay defense: same jti seen twice → second attempt rejected.

    The first call must succeed (otherwise the test isn't measuring
    replay), the second must fail with a replay-specific reason.
    """

    token = _mint(jti="jti-replay-singleton")

    verifier.verify(token)

    with pytest.raises(InvalidJwtError) as excinfo:
        verifier.verify(token)
    msg = str(excinfo.value).lower()
    assert "replay" in msg or "seen" in msg


def test_wrong_algorithm_is_rejected_even_if_signature_validates(
    verifier: JwtVerifier,
) -> None:
    """Algorithm-confusion guard — HS256 is the *only* allowed alg.

    Using HS384 is the cheapest way to confirm the verifier doesn't just
    accept "whatever PyJWT can decode". A real attack here would be HS/RS
    swap or alg=none; rejecting any non-HS256 covers all of those.
    """

    token = _mint(alg="HS512")

    with pytest.raises(InvalidJwtError):
        verifier.verify(token)


def test_alg_none_is_rejected(verifier: JwtVerifier) -> None:
    """Specific guard for the historical alg=none CVE family.

    If the verifier accepts unsigned tokens, the entire boundary is
    bypassable by anyone who can reach the endpoint. PyJWT defaults block
    this, but pinning the assertion means a future "loosen the verifier"
    refactor can't silently regress.
    """

    now = datetime.now(tz=UTC)
    payload = {
        "iss": "openemr-gateway",
        "aud": "clinical-copilot",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 300,
        "user_id": "user-42",
        "role": "physician",
        "patient_id": "patient-7",
        "scopes": [],
        "nonce": "nonce-abc",
        "jti": "jti-none",
    }
    unsigned = pyjwt.encode(payload, key="", algorithm="none")

    with pytest.raises(InvalidJwtError):
        verifier.verify(unsigned)


@pytest.mark.parametrize(
    "missing_claim",
    ["user_id", "role", "patient_id", "scopes", "nonce", "jti"],
)
def test_missing_required_claim_is_rejected(
    verifier: JwtVerifier,
    missing_claim: str,
) -> None:
    token = _mint(drop=(missing_claim,))

    with pytest.raises(InvalidJwtError) as excinfo:
        verifier.verify(token)
    assert missing_claim in str(excinfo.value)


def test_wrong_issuer_is_rejected(verifier: JwtVerifier) -> None:
    """A token minted for a different issuer must not validate even with a
    matching secret — guards against dev-prod secret crossover."""

    token = _mint(issuer="some-other-service")

    with pytest.raises(InvalidJwtError):
        verifier.verify(token)


def test_wrong_audience_is_rejected(verifier: JwtVerifier) -> None:
    token = _mint(audience="not-the-copilot")

    with pytest.raises(InvalidJwtError):
        verifier.verify(token)


def test_nonce_store_purges_expired_entries() -> None:
    """The replay store must drop entries past TTL so memory doesn't grow
    unbounded under sustained load. Equally important: an expired jti must
    be free to reappear (since the corresponding token is already rejected
    on exp grounds — the seen-set is a defense-in-depth layer)."""

    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
    advancing = {"now": now}

    def clock() -> datetime:
        return advancing["now"]

    store = NonceStore(ttl_seconds=300, clock=clock)
    assert store.claim("jti-x") is True
    assert store.claim("jti-x") is False  # within TTL → blocked

    advancing["now"] = now + timedelta(seconds=301)
    assert store.claim("jti-x") is True  # past TTL → re-claimable


def test_fastapi_dependency_returns_claims_on_valid_bearer() -> None:
    """Integration smoke: the FastAPI dependency parses the Authorization
    header, verifies, and injects the typed claims into a route. Any
    failure path returns 401 with a generic body — the test asserts the
    happy path here; failure-path coverage is in the next test."""

    app = FastAPI()
    verifier = JwtVerifier(
        secret=SECRET,
        replay_store=NonceStore(ttl_seconds=300),
    )

    @app.get("/whoami")
    def whoami(claims: ClinicianClaims = require_clinician_claims(verifier)) -> dict[str, str]:  # noqa: B008
        return {"user_id": claims.user_id, "patient_id": claims.patient_id}

    token = _mint(jti="jti-route-1")
    client = TestClient(app)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": "user-42", "patient_id": "patient-7"}


def test_fastapi_dependency_rejects_missing_or_bad_bearer() -> None:
    app = FastAPI()
    verifier = JwtVerifier(
        secret=SECRET,
        replay_store=NonceStore(ttl_seconds=300),
    )

    @app.get("/whoami")
    def whoami(claims: ClinicianClaims = require_clinician_claims(verifier)) -> dict[str, str]:  # noqa: B008
        return {"user_id": claims.user_id}

    client = TestClient(app)

    no_header = client.get("/whoami")
    assert no_header.status_code == 401

    wrong_scheme = client.get("/whoami", headers={"Authorization": "Basic abc"})
    assert wrong_scheme.status_code == 401

    garbage = client.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert garbage.status_code == 401

    # Verify the failure body is generic — no "InvalidSignatureError" or
    # claim-name leakage to the user.
    body = garbage.json()
    assert body == {"detail": "invalid token"}


def test_replay_store_is_thread_safe_under_concurrent_claims() -> None:
    """Multiple threads racing for the same jti — exactly one wins.

    FastAPI runs route handlers across threads/workers; if the replay
    store has a lost-update bug, two concurrent requests with the same
    jti both succeed, defeating the defense.
    """

    store = NonceStore(ttl_seconds=300)

    def attempt(_: int) -> bool:
        return store.claim("racey-jti")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(200)))

    # Exactly one True overall — every other attempt must be a False.
    assert sum(results) == 1


def test_nonce_store_default_clock_uses_real_time() -> None:
    """Sanity: omitting the clock injection still works (used in production
    where we want real wall time). A small sleep proves the default isn't
    frozen at import time."""

    store = NonceStore(ttl_seconds=1)
    assert store.claim("x") is True
    assert store.claim("x") is False
    time.sleep(1.1)
    assert store.claim("x") is True
