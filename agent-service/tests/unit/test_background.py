"""Pin the BackgroundRunner contract.

The runner is a thin loop over :meth:`DiscrepancyCache.get_flags`, so
the tests here use a stub cache rather than spinning up the engine —
the runner's only logic is the per-patient try/except and the
deduplication, and both are easier to assert against a recording stub.
"""

from __future__ import annotations

from typing import cast

from clinical_copilot.discrepancy.background import (
    BackgroundRunner,
    WarmFailure,
    WarmSummary,
)
from clinical_copilot.discrepancy.cache import DiscrepancyCache


class _RecordingCache:
    """Stub that mimics :class:`DiscrepancyCache.get_flags` only.

    Records every call so tests can assert dedup, and lets a per-patient
    error map raise on demand to exercise the failure-collection path.
    """

    def __init__(self, *, errors: dict[str, Exception] | None = None) -> None:
        self.calls: list[str] = []
        self._errors = errors or {}

    def get_flags(self, patient_id: str) -> list[object]:
        self.calls.append(patient_id)
        if patient_id in self._errors:
            raise self._errors[patient_id]
        return []


def _runner(cache: _RecordingCache) -> BackgroundRunner:
    # The runner only depends on .get_flags; cast keeps mypy happy
    # without tying the test's stub to the full DiscrepancyCache surface.
    return BackgroundRunner(cast(DiscrepancyCache, cache))


def test_warm_panel_warms_each_patient_once() -> None:
    cache = _RecordingCache()
    summary = _runner(cache).warm_panel(["101", "102", "103"])

    assert cache.calls == ["101", "102", "103"]
    assert summary == WarmSummary(warmed=3, failed=())


def test_warm_panel_deduplicates_repeated_ids() -> None:
    cache = _RecordingCache()
    summary = _runner(cache).warm_panel(["101", "102", "101", "102", "101"])

    assert cache.calls == ["101", "102"]
    assert summary.warmed == 2
    assert summary.failed == ()


def test_warm_panel_collects_per_patient_failures() -> None:
    cache = _RecordingCache(
        errors={
            "102": RuntimeError("chart load died"),
            "104": ValueError("bad chart shape"),
        },
    )
    summary = _runner(cache).warm_panel(["101", "102", "103", "104"])

    assert cache.calls == ["101", "102", "103", "104"]
    assert summary.warmed == 2
    # Reasons are exception type names — no message bleed-through.
    assert summary.failed == (
        WarmFailure(patient_id="102", reason="RuntimeError"),
        WarmFailure(patient_id="104", reason="ValueError"),
    )


def test_warm_panel_one_failure_does_not_abort_remaining_patients() -> None:
    cache = _RecordingCache(errors={"101": RuntimeError("first patient down")})
    summary = _runner(cache).warm_panel(["101", "102"])

    # If the first patient's exception aborted the loop we'd never see
    # the call for "102". The contract is best-effort across the panel.
    assert cache.calls == ["101", "102"]
    assert summary.warmed == 1
    assert summary.failed == (WarmFailure(patient_id="101", reason="RuntimeError"),)


def test_warm_panel_blank_patient_id_recorded_as_failure() -> None:
    cache = _RecordingCache()
    summary = _runner(cache).warm_panel(["101", "", "102"])

    # Blank id never reaches the cache — recording stub proves it.
    assert cache.calls == ["101", "102"]
    assert summary.warmed == 2
    assert summary.failed == (WarmFailure(patient_id="", reason="empty_patient_id"),)


def test_warm_panel_empty_input_returns_empty_summary() -> None:
    cache = _RecordingCache()
    summary = _runner(cache).warm_panel([])

    assert cache.calls == []
    assert summary == WarmSummary(warmed=0, failed=())
