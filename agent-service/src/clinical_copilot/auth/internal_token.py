"""Internal-token guard for service-to-service routes.

PR 15's warm and invalidate routes are *not* user-facing — the OpenEMR
PHP gateway calls them on patient-select and on write events to keep the
discrepancy cache fresh. They sit on the same FastAPI app as the chat
route but speak a different protocol:

* user-facing routes: HS256 bearer JWT minted from the clinician's
  session, verified by :class:`JwtVerifier`;
* service-to-service routes: shared-secret on the ``X-Internal-Token``
  header, compared in constant time against
  :attr:`Settings.internal_token`.

Two distinct secrets matter because the threat models differ. The user
JWT carries a per-clinician principal that the tool layer's RBAC binds
to a specific patient_id; the internal token authorises the gateway
process itself, so a leak compromises the cache surface but not any
specific clinician's PHI access. Rotating one without the other is a
common operational need; that's only possible if they're separate.

The header name is intentionally not ``Authorization`` so a misconfigured
PHP client that defaults to bearer-token auth can't accidentally satisfy
this gate. The PHP-side dispatcher (PR 15 Stage B) sets the header
explicitly; browser callers never see it because the gateway terminates
the user session before any internal route is reached.

Failure mode is 401 with a generic body — same shape as the user-JWT
gate so probe-and-classify against the route map is uninformative.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def require_internal_token(expected: str) -> Any:
    """Build a FastAPI dependency that gates a route on the shared secret.

    ``expected`` is the value from :attr:`Settings.internal_token`,
    captured by closure at app-build time so the dependency doesn't
    re-read settings on every request. An empty ``expected`` is a wiring
    bug — the production config requires the env var, and dev defaults
    to a non-empty placeholder; refusing to build the dep here surfaces
    the bug at app construction rather than letting an unguarded route
    ship.
    """

    if expected == "":
        raise ValueError("internal_token must be non-empty")

    def dependency(
        x_internal_token: Annotated[str | None, Header()] = None,
    ) -> None:
        # ``secrets.compare_digest`` is constant-time only when both
        # operands are the same length, so missing-header (None) and
        # length-mismatch both have to short-circuit to the same 401
        # before the comparison runs. The downside — leaking length
        # equality — is fine: the secret is opaque high-entropy bytes,
        # not a password the attacker is grinding character-by-character.
        if x_internal_token is None or len(x_internal_token) != len(expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            )
        if not secrets.compare_digest(x_internal_token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            )

    return Depends(dependency)
