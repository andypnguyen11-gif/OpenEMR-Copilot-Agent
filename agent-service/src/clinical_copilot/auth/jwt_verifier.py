"""Verifier for the PHP-gateway-issued boundary token.

The Co-Pilot trust model has two trust boundaries (ARCHITECTURE §4): the
``$_SESSION → JWT`` hop on the PHP side, and this verifier on the Python
side. We trust nothing the PHP gateway *says* without checking the
signature first; we trust nothing the model emits about the chart without
checking citations later. This file is the first half of that — the only
thing it does is turn an opaque bearer token into a typed
:class:`ClinicianClaims`, raising :class:`InvalidJwtError` on any
discrepancy.

Failure paths fold into a single exception type with a *reason* string so
the route handler can return a generic 401 to the caller while server-side
logs still record which check failed (signature / expired / replay /
malformed claim / wrong issuer / wrong audience). The reason string never
leaves the server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException, status
from pydantic import ValidationError

from clinical_copilot.auth.session import ClinicianClaims, NonceStore

if TYPE_CHECKING:
    from collections.abc import Callable

ALGORITHM = "HS256"
ISSUER = "openemr-gateway"
AUDIENCE = "clinical-copilot"
BEARER_HEADER_PARTS = 2

REQUIRED_CLAIMS = ("user_id", "role", "patient_id", "scopes", "nonce", "jti")

# Mapping from PyJWT exception type to the InvalidJwtError reason we surface.
# Order matters: more specific subclasses must come before InvalidTokenError,
# which is the catch-all parent.
_PYJWT_ERROR_REASONS: tuple[tuple[type[pyjwt.InvalidTokenError], str], ...] = (
    (pyjwt.ExpiredSignatureError, "token expired"),
    (pyjwt.ImmatureSignatureError, "token not yet valid"),
    (pyjwt.InvalidIssuerError, "issuer mismatch"),
    (pyjwt.InvalidAudienceError, "audience mismatch"),
    (pyjwt.InvalidSignatureError, "signature mismatch"),
    (pyjwt.InvalidAlgorithmError, "disallowed algorithm"),
)


class InvalidJwtError(Exception):
    """Raised on any verification failure.

    The message is a server-side diagnostic ("expired", "signature",
    "replay", "missing claim ``user_id``") and must not be surfaced to
    the user — every external response on this path is a generic 401.
    """


class JwtVerifier:
    """Stateless-from-the-outside HS256 verifier.

    The ``replay_store`` parameter is the only piece of mutable state the
    verifier owns; injecting it lets tests use a clock-frozen store and
    production wire in the process-wide :class:`NonceStore`.
    """

    def __init__(
        self,
        *,
        secret: str,
        replay_store: NonceStore,
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._replay_store = replay_store
        self._clock = clock or _utcnow

    def verify(self, token: str) -> ClinicianClaims:
        """Decode, validate, and return the typed claims.

        Order of checks matters: signature first, then standard claims
        (exp/iat/iss/aud) via PyJWT, then the Co-Pilot-specific claims
        and the replay-store update. The replay update is the *last*
        step so a token rejected on any earlier ground does not consume
        a jti slot.
        """

        payload = self._decode(token)
        self._reject_future_iat(payload)
        claims = self._parse_claims(payload)
        if not self._replay_store.claim(claims.jti):
            raise InvalidJwtError("replay detected: jti already seen")
        return claims

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            return pyjwt.decode(
                token,
                key=self._secret,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "jti"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
                leeway=0,
            )
        except pyjwt.MissingRequiredClaimError as exc:
            raise InvalidJwtError(f"missing claim {exc.claim}") from exc
        except pyjwt.InvalidTokenError as exc:
            for cls, reason in _PYJWT_ERROR_REASONS:
                if isinstance(exc, cls):
                    raise InvalidJwtError(reason) from exc
            # Catch-all for the remaining PyJWT failure modes (decode
            # errors, malformed segments, unknown alg). One generic
            # message is enough — the underlying exception is preserved
            # on __cause__ for logs.
            raise InvalidJwtError("invalid token") from exc

    def _reject_future_iat(self, payload: dict[str, Any]) -> None:
        # PyJWT does not enforce iat-in-the-future by default; this is the
        # belt-and-suspenders check the test suite asserts.
        iat = payload.get("iat")
        if isinstance(iat, int | float) and iat > self._clock().timestamp():
            raise InvalidJwtError("iat is in the future")

    def _parse_claims(self, payload: dict[str, Any]) -> ClinicianClaims:
        for required in REQUIRED_CLAIMS:
            if required not in payload:
                raise InvalidJwtError(f"missing claim {required}")

        scopes_raw = payload["scopes"]
        scopes = list(scopes_raw) if scopes_raw is not None else []
        try:
            return ClinicianClaims(
                user_id=str(payload["user_id"]),
                role=str(payload["role"]),
                patient_id=str(payload["patient_id"]),
                scopes=scopes,
                nonce=str(payload["nonce"]),
                jti=str(payload["jti"]),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise InvalidJwtError(f"malformed claim: {exc}") from exc


def require_clinician_claims(verifier: JwtVerifier) -> Any:
    """Build a FastAPI dependency that injects verified claims into a route.

    Failures land as a 401 with a generic body — internal reasons are
    available on the raised :class:`InvalidJwtError` for server-side logs
    only. The dependency is a closure over ``verifier`` so each route can
    bind to the process-wide instance configured at startup.
    """

    def dependency(
        authorization: Annotated[str | None, Header()] = None,
    ) -> ClinicianClaims:
        token = _extract_bearer(authorization)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            )
        try:
            return verifier.verify(token)
        except InvalidJwtError as exc:
            # Generic body protects against probe-and-classify attacks —
            # an attacker should not be able to learn whether their token
            # failed on signature, exp, or replay just from the response.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            ) from exc

    return Depends(dependency)


def _extract_bearer(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    parts = header_value.split(" ", 1)
    if len(parts) != BEARER_HEADER_PARTS or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
