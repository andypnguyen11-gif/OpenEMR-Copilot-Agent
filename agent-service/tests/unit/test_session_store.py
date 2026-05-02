"""Unit tests for the in-memory session store.

The store backs PR 9's multi-turn chat history. Coverage targets the
four properties the design rests on: composite-key isolation across
different principals, fresh-mint on unknown ids, TTL eviction, and
per-key serialization across concurrent same-session POSTs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.sessions import (
    SessionState,
    SessionStore,
    generate_session_id,
)


class _FakeClock:
    """Manually advanceable clock for TTL tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _claims(*, user_id: str = "dr-patel", patient_id: str = "101") -> ClinicianClaims:
    return ClinicianClaims(
        user_id=user_id,
        role="physician",
        patient_id=patient_id,
        scopes=["system/Condition.read"],
        nonce="n",
        jti=f"jti-{user_id}-{patient_id}",
    )


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock(datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def store(clock: _FakeClock) -> Iterator[SessionStore]:
    yield SessionStore(ttl_seconds=1800, clock=clock)


def test_get_or_create_with_none_session_id_mints_fresh_id(
    store: SessionStore,
    clock: _FakeClock,
) -> None:
    canonical_id, state = store.get_or_create(_claims(), None)
    try:
        assert canonical_id  # non-empty
        assert state.messages == []
        assert state.tool_results == []
    finally:
        store.release(_claims(), canonical_id)


def test_known_session_id_returns_prior_state(store: SessionStore) -> None:
    claims = _claims()
    first_id, _ = store.get_or_create(claims, None)
    persisted = SessionState(
        messages=[{"role": "user", "content": "first"}],
        tool_results=[],
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    )
    store.update(claims, first_id, persisted)

    second_id, state = store.get_or_create(claims, first_id)
    try:
        assert second_id == first_id
        assert state.messages == [{"role": "user", "content": "first"}]
    finally:
        store.release(claims, second_id)


def test_cross_user_id_mints_fresh_and_isolates_owner(store: SessionStore) -> None:
    owner = _claims(user_id="dr-patel")
    attacker = _claims(user_id="dr-evil")  # same patient_id, different user

    owner_id, _ = store.get_or_create(owner, None)
    store.update(
        owner,
        owner_id,
        SessionState(
            messages=[{"role": "user", "content": "owner-private"}],
            tool_results=[],
            last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        ),
    )

    attacker_canonical_id, attacker_state = store.get_or_create(attacker, owner_id)
    try:
        assert attacker_canonical_id != owner_id, (
            "store must mint a fresh id when the supplied id doesn't resolve "
            "under the caller's principal — echoing the stolen id would leak "
            "its existence"
        )
        assert attacker_state.messages == []
    finally:
        store.release(attacker, attacker_canonical_id)

    # Owner's session must be untouched.
    _, owner_state_after = store.get_or_create(owner, owner_id)
    try:
        assert owner_state_after.messages == [{"role": "user", "content": "owner-private"}]
    finally:
        store.release(owner, owner_id)


def test_cross_patient_id_mints_fresh_and_isolates_owner(store: SessionStore) -> None:
    a = _claims(user_id="dr-patel", patient_id="101")
    b = _claims(user_id="dr-patel", patient_id="999")  # same user, different patient

    a_id, _ = store.get_or_create(a, None)
    store.update(
        a,
        a_id,
        SessionState(
            messages=[{"role": "user", "content": "patient-101-context"}],
            tool_results=[],
            last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        ),
    )

    b_canonical_id, b_state = store.get_or_create(b, a_id)
    try:
        assert b_canonical_id != a_id
        assert b_state.messages == []
    finally:
        store.release(b, b_canonical_id)

    _, a_state_after = store.get_or_create(a, a_id)
    try:
        assert a_state_after.messages == [{"role": "user", "content": "patient-101-context"}]
    finally:
        store.release(a, a_id)


def test_unknown_session_id_under_correct_principal_mints_fresh(
    store: SessionStore,
) -> None:
    claims = _claims()
    foreign_id = generate_session_id()  # never inserted
    canonical_id, state = store.get_or_create(claims, foreign_id)
    try:
        assert canonical_id != foreign_id
        assert state.messages == []
    finally:
        store.release(claims, canonical_id)


def test_ttl_expiry_drops_session(store: SessionStore, clock: _FakeClock) -> None:
    claims = _claims()
    sid, _ = store.get_or_create(claims, None)
    store.update(
        claims,
        sid,
        SessionState(
            messages=[{"role": "user", "content": "soon-to-expire"}],
            tool_results=[],
            last_used_at=clock(),
        ),
    )

    clock.advance(1801)  # 1 second past TTL

    canonical_id, state = store.get_or_create(claims, sid)
    try:
        assert canonical_id != sid, "expired entry must not satisfy a lookup"
        assert state.messages == []
    finally:
        store.release(claims, canonical_id)


def test_delete_owner_returns_true_non_owner_returns_false(store: SessionStore) -> None:
    owner = _claims(user_id="dr-patel")
    attacker = _claims(user_id="dr-evil")

    sid, _ = store.get_or_create(owner, None)
    store.update(
        owner,
        sid,
        SessionState(
            messages=[{"role": "user", "content": "x"}],
            tool_results=[],
            last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        ),
    )

    assert store.delete(attacker, sid) is False, (
        "non-owner DELETE must not affect another principal's session"
    )

    # Owner's state still present.
    _, state = store.get_or_create(owner, sid)
    try:
        assert state.messages == [{"role": "user", "content": "x"}]
    finally:
        store.release(owner, sid)

    assert store.delete(owner, sid) is True

    # After successful delete, lookup mints fresh.
    canonical_id, state = store.get_or_create(owner, sid)
    try:
        assert canonical_id != sid
        assert state.messages == []
    finally:
        store.release(owner, canonical_id)


def test_concurrent_same_session_serializes_via_per_key_lock(
    store: SessionStore,
    clock: _FakeClock,
) -> None:
    """Two threads racing on the same composite key must serialize.

    Without per-key locking, both threads would read the empty initial
    state, each append one message, and last-write-wins would drop one.
    With the per-key lock, the second thread blocks at
    :meth:`get_or_create` until the first calls :meth:`update`.
    """

    claims = _claims()
    sid, _ = store.get_or_create(claims, None)
    store.update(claims, sid, SessionState(messages=[], tool_results=[], last_used_at=clock()))

    barrier = threading.Barrier(2)
    results: dict[str, list[dict[str, str]]] = {}

    def worker(label: str, message: str) -> None:
        barrier.wait()
        canonical_id, state = store.get_or_create(claims, sid)
        # Simulate a tiny critical section so the threads actually
        # interleave; without a yield the GIL release cadence makes the
        # race trivially win.
        time.sleep(0.05)
        new_messages = [*state.messages, {"role": "user", "content": message}]
        store.update(
            claims,
            canonical_id,
            SessionState(
                messages=new_messages,
                tool_results=state.tool_results,
                last_used_at=clock(),
            ),
        )
        results[label] = new_messages

    t1 = threading.Thread(target=worker, args=("a", "first"))
    t2 = threading.Thread(target=worker, args=("b", "second"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive(), "thread 1 deadlocked — per-key lock not released"
    assert not t2.is_alive(), "thread 2 deadlocked — per-key lock not released"

    _, final_state = store.get_or_create(claims, sid)
    try:
        contents = [m["content"] for m in final_state.messages]
        assert sorted(contents) == ["first", "second"], "both writes must be preserved; got " + str(
            contents
        )
    finally:
        store.release(claims, sid)


def test_release_drops_lock_without_writing(store: SessionStore, clock: _FakeClock) -> None:
    """Release path must drop the lock so subsequent get_or_create
    against the same key returns immediately."""

    claims = _claims()
    sid, _ = store.get_or_create(claims, None)
    store.release(claims, sid)

    # If the lock leaked, this would block forever.
    completed = threading.Event()

    def worker() -> None:
        canonical_id, _ = store.get_or_create(claims, sid)
        store.release(claims, canonical_id)
        completed.set()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2)
    assert completed.is_set(), "release() did not drop the per-key lock"
