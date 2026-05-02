"""Shared pytest fixtures for the unit-test package.

The PR-8 FHIR-backed tool tests share two fixtures:

* ``bridge`` — a process-wide :class:`AsyncBridge` lets the synchronous
  Tool layer drive the async FHIR client. Module-scoped so the daemon
  thread / event loop is shared across every test in a file rather
  than rebuilt per test.
* ``audit`` — a fresh in-memory :class:`AuditLogWriter` per test,
  matching the pattern in ``test_tools.py`` / ``test_tool_rbac.py``.

Both are placed in ``conftest.py`` so pytest auto-discovers them.
Re-importing fixtures from a helper module triggers ``F811``
(redefinition) on the parameter usage even with ``noqa`` markers.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clinical_copilot.runtime.async_bridge import AsyncBridge

from ._fhir_tool_helpers import RecordingAuditWriter


@pytest.fixture(scope="module")
def bridge() -> Iterator[AsyncBridge]:
    """One :class:`AsyncBridge` per test module.

    Module scope keeps daemon-thread / loop count bounded — six test
    modules x N tests would otherwise create N threads each. The
    bridge shuts down at module teardown so the loop closes cleanly.
    """

    bridge = AsyncBridge()
    try:
        yield bridge
    finally:
        bridge.shutdown()


@pytest.fixture
def audit() -> RecordingAuditWriter:
    return RecordingAuditWriter()
