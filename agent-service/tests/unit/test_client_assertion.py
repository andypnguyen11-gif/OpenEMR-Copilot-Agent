"""Unit tests for the SMART Backend Services JWT-bearer client_assertion minter.

This is the asymmetric-auth replacement for the static ``client_secret`` form
post that PR 5 originally used: OpenEMR's confidential-client OAuth2 endpoint
hard-rejects any registration with ``system/*`` scopes that lacks a registered
JWK (``src/RestControllers/AuthorizationController.php`` lines 312-317), so the
agent service must mint an RS384-signed JWT and post it as the
``client_assertion`` parameter per RFC 7523 §2.2.

The contract under test is what
``src/Common/Auth/OpenIDConnect/JWT/RsaSha384Signer.php`` and
``src/Common/Auth/OpenIDConnect/Grant/CustomClientCredentialsGrant.php`` accept
on a real OpenEMR instance:

- Algorithm is RS384 — not RS256, not HS256. The signer hard-codes
  ``ALGORITHM_ID = 'RS384'`` and rejects everything else before business logic
  runs (``RsaSha384Signer.php:42``).
- Header carries a ``kid`` matching the registered JWK
  (``RsaSha384Signer.php:106`` reads it via ``$key->getJSONWebKey($kid, 'RS384')``).
- ``iss`` and ``sub`` both equal the registered ``client_id``; ``aud`` equals
  the token endpoint URL — RFC 7523 §3 plus the OpenEMR audience check.
- ``exp`` is short (≤5 min) and ``jti`` is unique per call so a captured
  assertion can't be replayed.

These properties are auth-critical: a regression that silently downgrades to
HS256, omits ``kid``, reuses ``jti``, or sets a long-lived ``exp`` weakens the
trust boundary OpenEMR's JWT verifier was designed to enforce. Tests run
test-first per CLAUDE.md's high-risk path policy.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from clinical_copilot.auth.client_assertion import (
    DEFAULT_LIFETIME,
    ClientAssertionMinter,
)

CLIENT_ID = "agent-service-client"
TOKEN_URL = "https://openemr.example.test/oauth2/default/token"
KEY_ID = "agent-service-key-2026"


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    """Generate one RSA-2048 keypair per module — generation is the slow step.

    A 2048-bit key is cheap to mint but not free (~50-200ms on CI), so we
    cache for the module rather than the function. The pair is returned as
    PEM bytes because that's how the production ``OAUTH_PRIVATE_KEY_PEM``
    env var arrives — testing the closer-to-prod shape catches PEM parse
    bugs the cryptography-object shape would mask.
    """
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
def fixed_clock() -> Callable[[], datetime]:
    fixed = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def minter(private_pem: bytes, fixed_clock: Callable[[], datetime]) -> ClientAssertionMinter:
    return ClientAssertionMinter(
        private_key_pem=private_pem,
        key_id=KEY_ID,
        issuer=CLIENT_ID,
        audience=TOKEN_URL,
        clock=fixed_clock,
    )


def _decode(token: str, public_pem: bytes) -> tuple[dict[str, object], dict[str, object]]:
    """Decode without enforcing exp so frozen-clock tests don't drift over time.

    ``options.verify_exp=False`` is fine here because every test asserts
    ``exp`` explicitly; we don't want pyjwt's wall-clock check to leak
    into a fixed-clock test.
    """
    header = pyjwt.get_unverified_header(token)
    payload = pyjwt.decode(
        token,
        public_pem,
        algorithms=["RS384"],
        audience=TOKEN_URL,
        options={"verify_exp": False},
    )
    return header, payload


# ---------- algorithm and header ----------


def test_signs_with_rs384(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """RS384 is the only algorithm OpenEMR's signer accepts."""
    token = minter.mint()
    header, _ = _decode(token, public_pem)
    assert header["alg"] == "RS384"


def test_header_carries_configured_kid(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """OpenEMR resolves the JWK by ``kid`` (RsaSha384Signer.php:106)."""
    token = minter.mint()
    header, _ = _decode(token, public_pem)
    assert header["kid"] == KEY_ID


def test_header_carries_jwt_typ(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """``typ: JWT`` is RFC 7519 §5.1 boilerplate — pin it for explicitness."""
    token = minter.mint()
    header, _ = _decode(token, public_pem)
    assert header["typ"] == "JWT"


def test_signature_verifies_against_public_key(
    minter: ClientAssertionMinter, public_pem: bytes
) -> None:
    """A wrong public key must fail verification — the sanity check that
    proves the test isn't accidentally accepting unsigned tokens.
    """
    token = minter.mint()

    _decode(token, public_pem)

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_public = other_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(pyjwt.InvalidSignatureError):
        _decode(token, other_public)


# ---------- claims ----------


def test_iss_and_sub_equal_client_id(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """RFC 7523 §3 plus OpenEMR's claim check: iss == sub == client_id.

    Splitting iss from sub would make the assertion identify a different
    principal than the registered client, and OpenEMR would reject it.
    """
    token = minter.mint()
    _, payload = _decode(token, public_pem)
    assert payload["iss"] == CLIENT_ID
    assert payload["sub"] == CLIENT_ID


def test_aud_equals_token_url(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """RFC 7523 §3 audience check: must be the token endpoint URL.

    A wrong ``aud`` is what stops a captured assertion from being replayed
    against a different OAuth server.
    """
    token = minter.mint()
    _, payload = _decode(token, public_pem)
    assert payload["aud"] == TOKEN_URL


def test_iat_matches_clock(
    minter: ClientAssertionMinter,
    public_pem: bytes,
    fixed_clock: Callable[[], datetime],
) -> None:
    token = minter.mint()
    _, payload = _decode(token, public_pem)
    assert payload["iat"] == int(fixed_clock().timestamp())


def test_exp_is_iat_plus_default_lifetime(
    minter: ClientAssertionMinter,
    public_pem: bytes,
    fixed_clock: Callable[[], datetime],
) -> None:
    """Default 5 min lifetime: short enough that a leaked assertion is dead
    quickly, long enough to absorb clock skew and one retry.
    """
    token = minter.mint()
    _, payload = _decode(token, public_pem)
    expected_exp = int((fixed_clock() + DEFAULT_LIFETIME).timestamp())
    assert payload["exp"] == expected_exp
    assert timedelta(minutes=5) == DEFAULT_LIFETIME


def test_explicit_lifetime_is_honored(
    private_pem: bytes,
    public_pem: bytes,
    fixed_clock: Callable[[], datetime],
) -> None:
    custom = ClientAssertionMinter(
        private_key_pem=private_pem,
        key_id=KEY_ID,
        issuer=CLIENT_ID,
        audience=TOKEN_URL,
        clock=fixed_clock,
        lifetime=timedelta(minutes=2),
    )
    token = custom.mint()
    _, payload = _decode(token, public_pem)
    exp, iat = payload["exp"], payload["iat"]
    assert isinstance(exp, int)
    assert isinstance(iat, int)
    assert exp - iat == 120


def test_jti_is_unique_per_call(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """Replay defense: a captured assertion can be replayed within its
    ``exp`` window unless the server tracks ``jti``. We mint a fresh
    ``jti`` per call so two simultaneous calls never share one.
    """
    seen: set[str] = set()
    for _ in range(10):
        token = minter.mint()
        _, payload = _decode(token, public_pem)
        jti = payload["jti"]
        assert isinstance(jti, str)
        assert jti not in seen
        seen.add(jti)


def test_jti_is_uuid4_hex_format(minter: ClientAssertionMinter, public_pem: bytes) -> None:
    """Pin the ``jti`` shape so a refactor that replaces it with a counter
    (which would re-collide on process restart) is caught.
    """
    token = minter.mint()
    _, payload = _decode(token, public_pem)
    jti = payload["jti"]
    assert isinstance(jti, str)
    assert re.fullmatch(r"[0-9a-f]{32}", jti)


# ---------- constructor validation ----------


def test_constructor_rejects_empty_key_id(
    private_pem: bytes, fixed_clock: Callable[[], datetime]
) -> None:
    """A blank ``kid`` would force OpenEMR's signer into an undefined
    lookup; reject at construction so the error is loud.
    """
    with pytest.raises(ValueError):
        ClientAssertionMinter(
            private_key_pem=private_pem,
            key_id="",
            issuer=CLIENT_ID,
            audience=TOKEN_URL,
            clock=fixed_clock,
        )


def test_constructor_rejects_empty_issuer(
    private_pem: bytes, fixed_clock: Callable[[], datetime]
) -> None:
    with pytest.raises(ValueError):
        ClientAssertionMinter(
            private_key_pem=private_pem,
            key_id=KEY_ID,
            issuer="",
            audience=TOKEN_URL,
            clock=fixed_clock,
        )


def test_constructor_rejects_empty_audience(
    private_pem: bytes, fixed_clock: Callable[[], datetime]
) -> None:
    with pytest.raises(ValueError):
        ClientAssertionMinter(
            private_key_pem=private_pem,
            key_id=KEY_ID,
            issuer=CLIENT_ID,
            audience="",
            clock=fixed_clock,
        )


def test_constructor_rejects_empty_private_key(
    fixed_clock: Callable[[], datetime],
) -> None:
    with pytest.raises(ValueError):
        ClientAssertionMinter(
            private_key_pem=b"",
            key_id=KEY_ID,
            issuer=CLIENT_ID,
            audience=TOKEN_URL,
            clock=fixed_clock,
        )


def test_constructor_rejects_non_positive_lifetime(
    private_pem: bytes, fixed_clock: Callable[[], datetime]
) -> None:
    """A non-positive lifetime would mint an already-expired assertion."""
    with pytest.raises(ValueError):
        ClientAssertionMinter(
            private_key_pem=private_pem,
            key_id=KEY_ID,
            issuer=CLIENT_ID,
            audience=TOKEN_URL,
            clock=fixed_clock,
            lifetime=timedelta(seconds=0),
        )
