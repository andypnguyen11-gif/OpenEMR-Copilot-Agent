"""In-memory, per-replica session store for multi-turn chat history.

PRD §3 wants conversation continuity within a session and a hard drop
on session end (panel close, patient switch). The store keys state by
the composite ``(user_id, patient_id, session_id)`` triple — both
``user_id`` and ``patient_id`` come from the verified JWT
(:class:`ClinicianClaims`), not from any client-supplied body. A guessed
or stolen ``session_id`` cannot retrieve another principal's history
because the lookup tuple itself differs.

A request that arrives with an unknown ``session_id`` (server-restart,
TTL eviction, or a foreign id) gets a freshly-minted server UUID — the
client's id is discarded. Echoing the client's id would (a) make a junk
id sticky and (b) hand an attacker an existence oracle when paired with
other probes. The wire shape is "client may send a hint; server returns
the canonical id."

Concurrency. A single request's read-modify-write spans the LLM loop
(potentially seconds), so two concurrent POSTs on the same composite
key would race last-write-wins. The store hands out a per-key
:class:`threading.Lock` from :meth:`SessionStore.get_or_create`; paired
:meth:`update` or :meth:`release` calls drop it. The chat UI's submit
button disables during a request (``chat.js`` ``submitBtn.disabled =
true``), so this is belt-and-suspenders for two-tab use, proxy retries,
and concurrent test paths.

Single replica. State lives in-process; horizontal scale would require
shared storage (Redis or sticky routing). Same posture as
:class:`NonceStore` — explicitly chosen over Redis for MVP per
ARCHITECTURE §6. Document if/when that changes.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from clinical_copilot.auth.session import ClinicianClaims
    from clinical_copilot.tools.records import ToolResult

DEFAULT_TTL_SECONDS = 30 * 60

SessionKey = tuple[str, str, str]


def generate_session_id() -> str:
    """Return a fresh UUID4 hex for use as a server-canonical session id.

    Split out so tests can monkey-patch via :mod:`unittest.mock`.
    """

    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class SessionState:
    """One session's persisted conversation context.

    ``messages`` is the Anthropic-shape message list (each item a dict
    with ``role`` and ``content``) that the orchestrator hands to the
    LLM on the next turn. ``tool_results`` accumulates every successful
    tool call's :class:`ToolResult` so the verification middleware can
    resolve citations on subsequent turns. ``last_used_at`` is touched
    on every write — TTL eviction is computed against this.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    last_used_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class SessionStore:
    """Process-local TTL session store with per-key serialization.

    The contract a caller implements between
    :meth:`get_or_create` and the matching :meth:`update` / :meth:`release`:

    1. Call :meth:`get_or_create` to acquire the per-key lock and read
       the state. The returned ``canonical_id`` may differ from the id
       the caller supplied — always use the canonical id thereafter.
    2. Do the work. The lock is held the whole time.
    3. Call :meth:`update` to write back state and drop the lock, or
       :meth:`release` to drop the lock without writing.

    Failing to call either leaks the lock and stalls future requests on
    that key until the process restarts. :class:`Orchestrator.run`
    wraps the run in ``try / finally`` to guarantee one of the two
    fires.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _utcnow
        self._global_lock = threading.Lock()
        self._states: dict[SessionKey, SessionState] = {}
        self._locks: dict[SessionKey, threading.Lock] = {}

    def get_or_create(
        self,
        claims: ClinicianClaims,
        session_id: str | None,
    ) -> tuple[str, SessionState]:
        """Resolve a session and acquire its per-key lock.

        Returns ``(canonical_id, state)``. ``canonical_id`` is the
        server's id for this session — for first-time-or-unknown ids
        it's a fresh UUID, otherwise it's the supplied id. Caller must
        eventually call :meth:`update` or :meth:`release` against this
        canonical id.
        """

        with self._global_lock:
            self._purge_expired_locked()
            canonical_id = self._resolve_canonical_id_locked(claims, session_id)
            key = self._key(claims, canonical_id)
            per_key_lock = self._locks.setdefault(key, threading.Lock())

        # Acquire outside the global lock so a long-held per-key lock
        # doesn't block other sessions from making progress.
        per_key_lock.acquire()

        with self._global_lock:
            self._purge_expired_locked()
            state = self._states.get(key)
            if state is None:
                state = SessionState(last_used_at=self._clock())
        return canonical_id, state

    def update(
        self,
        claims: ClinicianClaims,
        session_id: str,
        state: SessionState,
    ) -> None:
        """Write ``state`` and drop the per-key lock acquired by
        :meth:`get_or_create`.

        ``last_used_at`` is overwritten with the store's clock — TTL
        decisions reference the store's notion of time, not the caller's.
        Callers can construct :class:`SessionState` without supplying a
        timestamp.
        """

        key = self._key(claims, session_id)
        bumped = SessionState(
            messages=state.messages,
            tool_results=state.tool_results,
            last_used_at=self._clock(),
        )
        with self._global_lock:
            self._states[key] = bumped
            per_key_lock = self._locks.get(key)
        self._safe_release(per_key_lock)

    def release(
        self,
        claims: ClinicianClaims,
        session_id: str,
    ) -> None:
        """Drop the per-key lock without writing — for callers that
        bail (uncaught exception, etc.). State is preserved as-is."""

        key = self._key(claims, session_id)
        with self._global_lock:
            per_key_lock = self._locks.get(key)
        self._safe_release(per_key_lock)

    def delete(
        self,
        claims: ClinicianClaims,
        session_id: str,
    ) -> bool:
        """Remove a session under the calling principal.

        Returns True if a session existed under the composite key and
        was removed, False otherwise. A different principal calling
        with the same ``session_id`` cannot affect the original
        owner's state because the lookup tuple itself differs — DELETE
        always 404s for a non-owner instead of returning 401, which
        would leak whether the session exists somewhere else.

        Best-effort against an in-flight run on the same key: if a run
        is mid-loop and writes back state via :meth:`update` after this
        call, the entry resurrects until TTL. The chat UI serializes
        DELETE behind the submit button so this race is theoretical.
        """

        key = self._key(claims, session_id)
        with self._global_lock:
            existed = key in self._states
            self._states.pop(key, None)
            # Leave self._locks[key] in place — another thread may be
            # waiting on it. The lock entry GCs naturally when no one
            # references it after the next purge.
        return existed

    def _resolve_canonical_id_locked(
        self,
        claims: ClinicianClaims,
        session_id: str | None,
    ) -> str:
        """Decide the canonical id for this request.

        - ``None`` or unknown id under this principal → mint a fresh
          server UUID. The caller's id (if any) is discarded.
        - Known id under this principal → echo back the supplied id.

        Caller must already hold the global lock.
        """

        if session_id is None:
            return generate_session_id()
        candidate_key = (claims.user_id, claims.patient_id, session_id)
        if candidate_key in self._states:
            return session_id
        return generate_session_id()

    def _key(self, claims: ClinicianClaims, session_id: str) -> SessionKey:
        return (claims.user_id, claims.patient_id, session_id)

    def _purge_expired_locked(self) -> None:
        """Drop entries whose TTL has elapsed.

        Linear in the size of the store. Request volume keeps the
        store small (one entry per active chat session, evicted on
        clear / patient switch / TTL), so a heap is unnecessary
        complexity. Mirrors :class:`NonceStore._purge_expired`.

        Caller must already hold the global lock.
        """

        now_ts = self._clock().timestamp()
        cutoff = now_ts - self._ttl_seconds
        expired_keys = [
            key for key, state in self._states.items() if state.last_used_at.timestamp() <= cutoff
        ]
        for key in expired_keys:
            del self._states[key]
            self._locks.pop(key, None)

    @staticmethod
    def _safe_release(lock: threading.Lock | None) -> None:
        # Already-released → silent no-op. A double-release is a caller
        # bug (mismatched try/finally), but raising here would mask the
        # original error path that triggered the second release.
        if lock is None:
            return
        with contextlib.suppress(RuntimeError):
            lock.release()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
