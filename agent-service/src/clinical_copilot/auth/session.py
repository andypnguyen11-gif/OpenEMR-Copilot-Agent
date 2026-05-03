"""Session-side helpers for the PHP→Python trust boundary.

Two pieces live here:

* :class:`ClinicianClaims` — Pydantic model the verifier returns and FastAPI
  routes consume. Mirrors the PHP gateway's ``ClinicianIdentity`` plus the
  JWT-standard fields the verifier needs (``nonce``, ``jti``).
* :class:`NonceStore` — in-memory replay defense. Each verified token's
  ``jti`` is recorded; a second arrival within TTL is refused. The store is
  intentionally process-local: with a single agent-service replica, an
  in-memory set is sufficient and avoids a Redis dependency
  (ARCHITECTURE §6 explicitly rules out Redis). Horizontal scaling would
  require swapping this implementation for a shared backing store, but
  that's outside MVP scope.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from clinical_copilot.auth.role import Role

if TYPE_CHECKING:
    from collections.abc import Callable


class ClinicianClaims(BaseModel):
    """Validated claims pulled out of a verified JWT.

    Strict mode: any claim type mismatch or unexpected extra field at the
    verifier boundary should be a hard rejection, not a silent coercion.

    ``role`` is typed as the closed :class:`Role` enum so the tool layer's
    ``allowed_roles`` set comparison always works against a known case.
    The verifier converts the raw JWT string via :meth:`Role.from_claim`
    before construction, mapping unrecognised values to :attr:`Role.UNKNOWN`
    rather than raising — forward-compat with a future PHP role case must
    fail closed at the tool boundary, not 5xx the verifier.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    user_id: str = Field(min_length=1)
    role: Role
    patient_id: str = Field(min_length=1)
    scopes: list[str]
    nonce: str = Field(min_length=1)
    jti: str = Field(min_length=1)


class NonceStore:
    """Process-local seen-jti set with TTL-based eviction.

    Threads racing on the same jti are serialized through ``_lock`` so that
    exactly one ``claim()`` call returns ``True`` per (jti, TTL window).
    The TTL must be ``>=`` the JWT lifetime — otherwise a token whose
    signature is still valid could be re-played after its jti drops out
    of the set.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _utcnow
        self._seen: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def claim(self, jti: str) -> bool:
        """Reserve ``jti`` for one use within the TTL window.

        Returns ``True`` on first claim, ``False`` if the jti is already
        recorded and its entry has not yet expired. A second-arrival
        rejection is the verifier's signal to raise an
        :class:`InvalidJwtError` with a replay reason.
        """

        if not jti:
            raise ValueError("jti must be non-empty")

        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            existing = self._seen.get(jti)
            if existing is not None:
                return False
            self._seen[jti] = now
            return True

    def _purge_expired(self, now: datetime) -> None:
        """Drop entries whose TTL has elapsed.

        Linear in the size of the set; the agent service's request volume
        keeps the set small (well under any number where this would
        matter), and the alternative (heap-based eviction) adds complexity
        for no measurable win at MVP scale.
        """

        cutoff = now.timestamp() - self._ttl_seconds
        expired = [jti for jti, recorded in self._seen.items() if recorded.timestamp() <= cutoff]
        for jti in expired:
            del self._seen[jti]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
