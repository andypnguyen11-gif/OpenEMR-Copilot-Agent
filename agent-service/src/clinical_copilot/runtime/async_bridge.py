"""Synchronous bridge into a long-lived asyncio event loop.

The Tool ABC (PR 7) is synchronous — :meth:`Tool._run` returns a
``Sequence[AnyRecord]``, not a coroutine. The :class:`FhirClient` (PR 6)
is asynchronous — every public method is ``async def`` and the
underlying ``httpx.AsyncClient`` is bound to the loop it was constructed
in. PR 8 wires the FHIR client into FHIR-backed tools without rewriting
either contract; this module is the seam.

How it works:

* One :class:`AsyncBridge` per process, owned by the composition root
  (:mod:`clinical_copilot.app_state`). Construction starts a daemon
  thread running ``loop.run_forever()`` and blocks until the loop is
  ready, so the first :meth:`run` call doesn't race the thread startup.
* Every coroutine submitted via :meth:`run` is dispatched onto that
  loop with :func:`asyncio.run_coroutine_threadsafe`; the calling thread
  blocks on the resulting :class:`concurrent.futures.Future` until the
  coroutine resolves.
* Long-lived async resources (the shared ``httpx.AsyncClient``,
  :class:`OAuthClient`, :class:`FhirClient`) are constructed *inside*
  the bridge loop via :meth:`run` so their event-loop binding matches
  the thread that drives them. Constructing the client on the main
  thread and using it through the bridge would dead-lock on first I/O
  because httpx-async ties internal locks to the loop where they were
  created.

Failure modes:

* The wrapped coroutine raising propagates the original exception
  through ``Future.result()``. The bridge does not translate or wrap
  errors — :class:`FhirError`, :class:`OAuthError` etc. surface
  unchanged so callers can pattern-match on them as before.
* Construction of the bridge fails closed: ``threading.Event.wait()``
  blocks until the loop thread reports ready; if the thread crashes
  during startup the wait would hang, but the daemon flag means the
  process exits with the parent and the failure surfaces as a startup
  hang in the deploy logs (loud) rather than a silent dead-lock at
  request time.

Why a long-lived thread instead of ``asyncio.run`` per call:
``asyncio.run`` would build a fresh loop per call, which is incompatible
with sharing a single ``httpx.AsyncClient`` across tools (the client's
connection pool would be torn down after each FHIR call). One bridge,
one loop, one connection pool — the cost is one daemon thread for the
lifetime of the process.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


class AsyncBridge:
    """Long-lived ``asyncio`` event loop on a daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="clinical-copilot-async-bridge",
            daemon=True,
        )
        self._thread.start()
        # Block construction until the loop is set on its thread. Without
        # this, a fast-path caller that hands run() the very first
        # coroutine can race the loop's startup and have its future
        # silently never schedule.
        self._ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The bridge's event loop.

        Exposed so the composition root can construct loop-bound
        resources (``httpx.AsyncClient``) inside the bridge by submitting
        a builder coroutine — see module docstring.
        """

        return self._loop

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Submit ``coro`` to the bridge loop and block on its result.

        Re-raises whatever the coroutine raised; transport / FHIR / OAuth
        exceptions pass through unchanged so the tool layer's ``except
        FhirError`` clauses keep working.
        """

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def shutdown(self) -> None:
        """Stop the loop and join the thread.

        Used by tests to keep the test process from accumulating loops.
        Production code never calls this — the daemon thread tears down
        with the process.
        """

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()
