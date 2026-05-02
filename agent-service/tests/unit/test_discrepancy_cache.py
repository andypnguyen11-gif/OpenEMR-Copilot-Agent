"""Unit tests for the two-tier discrepancy cache (PR 14).

Cache surface contract:

* Miss → compute via ``chart_provider`` + ``engine``, persist to both
  tiers, return.
* Hit in the in-process tier within TTL → return without touching the
  durable tier.
* Hit in the durable tier (cold in-process) → hydrate in-process and
  return.
* TTL expiry on the in-process row → fall through to durable / recompute.
* :meth:`DiscrepancyCache.invalidate` drops both tiers (PR 15 hook).
* Restart (new cache instance, same DB) preserves the durable row, so the
  next read serves from durable instead of recomputing.

Stubs replace the chart provider and engine so we can count recomputes
without standing up the real rule packs.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_engine_from_url, create_session_factory
from clinical_copilot.db.models import DiscrepancyCacheRow
from clinical_copilot.discrepancy.cache import DiscrepancyCache
from clinical_copilot.discrepancy.chart_provider import ChartProvider
from clinical_copilot.discrepancy.engine import DiscrepancyEngine, PatientChart
from clinical_copilot.tools.records import FlagRecord

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker


PATIENT_ID = "patient-101"


class _StubChartProvider(ChartProvider):
    """Returns the same empty chart for any ``patient_id``.

    Real chart contents don't matter — the engine stub ignores them and
    the cache only forwards them. Counting calls to ``load_chart`` lets
    us verify the cache short-circuits on hits.
    """

    def __init__(self) -> None:
        self.load_calls = 0

    def load_chart(self, patient_id: str) -> PatientChart:
        self.load_calls += 1
        return PatientChart(patient_id=patient_id)


class _StubEngine(DiscrepancyEngine):
    """Engine subclass that returns canned flags and counts evaluations.

    Subclassing the production class keeps the cache's static type for
    ``engine`` honest — ducked-typed stubs would force the cache to
    weaken its parameter type to satisfy mypy.
    """

    def __init__(self, flag: FlagRecord | None = None) -> None:
        super().__init__(rules=())
        self.eval_calls = 0
        self._flag = flag or _make_flag("rule-1", "consistency")

    def evaluate(self, chart: PatientChart) -> list[FlagRecord]:
        del chart
        self.eval_calls += 1
        return [self._flag]


class _EmptyEngine(DiscrepancyEngine):
    """Engine subclass that always returns zero flags."""

    def __init__(self) -> None:
        super().__init__(rules=())
        self.eval_calls = 0

    def evaluate(self, chart: PatientChart) -> list[FlagRecord]:
        del chart
        self.eval_calls += 1
        return []


class _Clock:
    """Mutable clock — call :meth:`advance` between cache reads."""

    def __init__(self, *, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _make_flag(rule_id: str, category: str) -> FlagRecord:
    return FlagRecord(
        source_id=f"flag/{rule_id}/abc",
        rule_id=rule_id,
        category=category,
        rationale="stub",
        referenced_source_ids=["src-1"],
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite with the schema applied."""

    eng = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def test_miss_recomputes_and_returns_flags(
    session_factory: sessionmaker[Session],
) -> None:
    chart = _StubChartProvider()
    eng = _StubEngine()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=session_factory,
        clock=_Clock(),
    )

    flags = cache.get_flags(PATIENT_ID)

    assert [f.rule_id for f in flags] == ["rule-1"]
    assert chart.load_calls == 1
    assert eng.eval_calls == 1


def test_in_process_hit_within_ttl_skips_recompute(
    session_factory: sessionmaker[Session],
) -> None:
    chart = _StubChartProvider()
    eng = _StubEngine()
    clock = _Clock()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=session_factory,
        ttl_seconds=600,
        clock=clock,
    )

    cache.get_flags(PATIENT_ID)
    clock.advance(60)
    second = cache.get_flags(PATIENT_ID)

    # Still inside TTL → in-process serves the second read.
    assert eng.eval_calls == 1
    assert chart.load_calls == 1
    assert [f.rule_id for f in second] == ["rule-1"]


def test_ttl_expiry_forces_recompute(
    session_factory: sessionmaker[Session],
) -> None:
    chart = _StubChartProvider()
    eng = _StubEngine()
    clock = _Clock()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=session_factory,
        ttl_seconds=60,
        clock=clock,
    )

    cache.get_flags(PATIENT_ID)
    # Advance past TTL on both tiers (durable shares the same expires_at).
    clock.advance(120)
    cache.get_flags(PATIENT_ID)

    assert eng.eval_calls == 2
    assert chart.load_calls == 2


def test_durable_hit_hydrates_in_process_after_restart(
    session_factory: sessionmaker[Session],
) -> None:
    """Cache instance #2 over the same DB serves from the durable tier
    without re-evaluating the engine — the restart-preservation
    guarantee from TASKS.md PR 14 acceptance.
    """

    chart_a = _StubChartProvider()
    eng_a = _StubEngine()
    clock = _Clock()
    cache_a = DiscrepancyCache(
        chart_provider=chart_a,
        engine=eng_a,
        session_factory=session_factory,
        ttl_seconds=600,
        clock=clock,
    )
    cache_a.get_flags(PATIENT_ID)

    chart_b = _StubChartProvider()
    eng_b = _StubEngine()
    cache_b = DiscrepancyCache(
        chart_provider=chart_b,
        engine=eng_b,
        session_factory=session_factory,
        ttl_seconds=600,
        clock=clock,
    )

    second = cache_b.get_flags(PATIENT_ID)

    assert [f.rule_id for f in second] == ["rule-1"]
    # Cache B never recomputed — the durable row served the read.
    assert eng_b.eval_calls == 0
    assert chart_b.load_calls == 0


def test_durable_row_persists_across_engine_dispose() -> None:
    """File-backed SQLite verifies the row survives engine teardown.

    The in-memory engine fixture above proves cross-cache hits within a
    process; this test pins the durability story by recreating the
    SQLAlchemy ``Engine`` itself between writes and reads.
    """

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'cache.db'}"

        eng_db_a = create_engine_from_url(url)
        Base.metadata.create_all(eng_db_a)
        factory_a = create_session_factory(eng_db_a)
        try:
            cache_a = DiscrepancyCache(
                chart_provider=_StubChartProvider(),
                engine=_StubEngine(),
                session_factory=factory_a,
                ttl_seconds=600,
                clock=_Clock(),
            )
            cache_a.get_flags(PATIENT_ID)
        finally:
            eng_db_a.dispose()

        eng_db_b = create_engine_from_url(url)
        try:
            factory_b = create_session_factory(eng_db_b)
            chart_b = _StubChartProvider()
            engine_b = _StubEngine()
            cache_b = DiscrepancyCache(
                chart_provider=chart_b,
                engine=engine_b,
                session_factory=factory_b,
                ttl_seconds=600,
                clock=_Clock(),
            )
            flags = cache_b.get_flags(PATIENT_ID)
        finally:
            eng_db_b.dispose()

        assert [f.rule_id for f in flags] == ["rule-1"]
        assert engine_b.eval_calls == 0


def test_invalidate_drops_both_tiers(
    session_factory: sessionmaker[Session],
) -> None:
    chart = _StubChartProvider()
    eng = _StubEngine()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=session_factory,
        clock=_Clock(),
    )

    cache.get_flags(PATIENT_ID)
    cache.invalidate(PATIENT_ID)

    # Durable tier row is gone.
    with session_factory() as s:
        rows = s.query(DiscrepancyCacheRow).all()
        assert rows == []

    # Next read recomputes — in-process tier was cleared too.
    cache.get_flags(PATIENT_ID)
    assert eng.eval_calls == 2


def test_invalidate_unknown_patient_is_noop(
    session_factory: sessionmaker[Session],
) -> None:
    """PR 15's write-path hooks fire on every chart-mutating event,
    including ones for patients the cache has never seen. Invalidation
    must not error in that case.
    """

    cache = DiscrepancyCache(
        chart_provider=_StubChartProvider(),
        engine=_StubEngine(),
        session_factory=session_factory,
    )

    cache.invalidate("never-cached")  # must not raise


def test_in_process_only_mode_skips_durable_tier() -> None:
    """``session_factory=None`` keeps everything in memory.

    The fixture-test path uses this — no DB needed for tests that only
    care about hot-cache behavior.
    """

    chart = _StubChartProvider()
    eng = _StubEngine()
    clock = _Clock()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=None,
        ttl_seconds=600,
        clock=clock,
    )

    cache.get_flags(PATIENT_ID)
    cache.get_flags(PATIENT_ID)
    assert eng.eval_calls == 1

    cache.invalidate(PATIENT_ID)
    cache.get_flags(PATIENT_ID)
    assert eng.eval_calls == 2


def test_get_flags_rejects_empty_patient_id() -> None:
    cache = DiscrepancyCache(
        chart_provider=_StubChartProvider(),
        engine=_StubEngine(),
    )
    with pytest.raises(ValueError, match="patient_id"):
        cache.get_flags("")


def test_invalidate_rejects_empty_patient_id() -> None:
    cache = DiscrepancyCache(
        chart_provider=_StubChartProvider(),
        engine=_StubEngine(),
    )
    with pytest.raises(ValueError, match="patient_id"):
        cache.invalidate("")


def test_constructor_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        DiscrepancyCache(
            chart_provider=_StubChartProvider(),
            engine=_StubEngine(),
            ttl_seconds=0,
        )


def test_recompute_caches_empty_flag_list(
    session_factory: sessionmaker[Session],
) -> None:
    """An evaluation that returns zero flags is still a hit on the
    second read — we cache the empty result, not just non-empty ones.
    Otherwise a clean chart pays the recompute cost on every read.
    """

    chart = _StubChartProvider()
    eng = _EmptyEngine()
    cache = DiscrepancyCache(
        chart_provider=chart,
        engine=eng,
        session_factory=session_factory,
        clock=_Clock(),
    )

    assert cache.get_flags(PATIENT_ID) == []
    assert cache.get_flags(PATIENT_ID) == []
    assert eng.eval_calls == 1
