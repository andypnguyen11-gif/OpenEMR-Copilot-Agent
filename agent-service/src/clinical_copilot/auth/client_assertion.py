"""SMART Backend Services JWT-bearer client_assertion minter.

OpenEMR's confidential-client OAuth2 endpoint hard-rejects any registration
with ``system/*`` scopes that lacks a registered JWK
(``src/RestControllers/AuthorizationController.php`` lines 312-317), so the
agent service authenticates to the token endpoint with an RS384-signed JWT
posted as the ``client_assertion`` parameter per RFC 7523 §2.2 — what
``src/Common/Auth/OpenIDConnect/Grant/CustomClientCredentialsGrant.php`` (and
the ``RsaSha384Signer`` it delegates to) accepts on a real instance.

Algorithm is RS384 only. OpenEMR ships a single signer
(``src/Common/Auth/OpenIDConnect/JWT/RsaSha384Signer.php`` line 42 —
``ALGORITHM_ID = 'RS384'``) and rejects every other algorithm before business
logic runs. The JWT header must include a ``kid`` matching the registered JWK
(``RsaSha384Signer.php:106``).

This module is the trust boundary between the agent service's identity
material (private key in ``OAUTH_PRIVATE_KEY_PEM``) and the OpenEMR token
server. It mints; it does not cache, retry, or know about HTTP. The
:class:`OAuthClient` calls it once per token fetch.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

ALGORITHM = "RS384"

# Default 5-minute lifetime: short enough that a leaked assertion is dead
# quickly, long enough to absorb clock skew and one retry. Override per
# instance if a deployment needs a different window — but never raise it
# above ~10 min without an explicit security review.
DEFAULT_LIFETIME = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class ClientAssertionMinter:
    """Pure RS384 JWT-bearer client_assertion minter.

    Stateless across calls except for the per-call ``jti`` (UUID4). Every
    field is validated at construction so a misconfigured deploy fails at
    boot rather than at the first token fetch.

    Construction parameters:

    - ``private_key_pem`` — PKCS8 PEM bytes of the RSA private key matching
      the ``kid`` registered with OpenEMR. Multi-line PEM is the format
      Railway accepts in env vars.
    - ``key_id`` — the ``kid`` in the registered JWK; embedded in every
      minted assertion's header so OpenEMR can resolve the public key.
    - ``issuer`` — registered ``client_id``; populates both ``iss`` and
      ``sub`` per RFC 7523 §3.
    - ``audience`` — token endpoint URL; populates ``aud`` so a captured
      assertion can't be replayed at a different OAuth server.
    - ``clock`` — injected for deterministic tests; defaults to UTC wall
      time in production (PSR-20-style time injection per CLAUDE.md).
    - ``lifetime`` — assertion validity window; default 5 min.
    """

    private_key_pem: bytes
    key_id: str
    issuer: str
    audience: str
    clock: Callable[[], datetime] = _utcnow
    lifetime: timedelta = DEFAULT_LIFETIME

    def __post_init__(self) -> None:
        if not self.private_key_pem:
            raise ValueError("private_key_pem must be non-empty")
        if not self.key_id:
            raise ValueError("key_id must be non-empty")
        if not self.issuer:
            raise ValueError("issuer must be non-empty")
        if not self.audience:
            raise ValueError("audience must be non-empty")
        if self.lifetime <= timedelta(0):
            raise ValueError("lifetime must be positive")

    def mint(self) -> str:
        """Return a fresh RS384-signed assertion ready to POST as ``client_assertion``.

        Per-call ``jti`` (UUID4) is the replay defense: even within the
        ``exp`` window, OpenEMR's deduper rejects a second assertion with
        the same ``jti``.
        """
        now = self.clock()
        payload: dict[str, object] = {
            "iss": self.issuer,
            "sub": self.issuer,
            "aud": self.audience,
            "iat": int(now.timestamp()),
            "exp": int((now + self.lifetime).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return pyjwt.encode(
            payload,
            self.private_key_pem,
            algorithm=ALGORITHM,
            headers={"kid": self.key_id, "typ": "JWT"},
        )
