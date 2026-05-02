"""Two-tier read-through cache for discrepancy flags (PR 14).

Tier 1 — in-process ``dict`` keyed by ``patient_id``, guarded by a single
lock. Hot reads inside the same replica's lifetime never touch the DB.

Tier 2 — Postgres row in ``discrepancy_cache`` (or SQLite in dev). Survives
a process restart and a deploy. Optional: callers without a session
factory get an in-process-only cache, which is fine for tests that only
exercise hot-path behaviour and for the `from_fixture` registry path that
runs without a DB.

Read-through algorithm:

1. Tier 1 hit + not expired → return.
2. Tier 2 hit + not expired → hydrate Tier 1 and return.
3. Miss → call :meth:`ChartProvider.load_chart` + ``engine.evaluate``,
   write to Tier 2 (upsert) and Tier 1, return.

TTL is 30 minutes by default — the upper end of ARCHITECTURE §6.4's
15-to-30-minute envelope. PR 15's invalidation hooks (med save, lab post, allergy
update, note sign) call :meth:`invalidate` to drop both tiers for a
patient when the chart changes underneath us.

Concurrency note. Two simultaneous misses for the same ``patient_id``
will both recompute and both upsert. The recompute is pure-functional
over the chart and the rule output is idempotent, so a duplicate compute
is wasted CPU — not a correctness issue. We deliberately avoid a
per-key lock here because rule evaluation is fast (sub-100ms today) and
single-flight machinery would add complexity that isn't worth it at MVP
volumes. If profiling later shows recompute storms, lift the in-process
lock to a per-key map the way :class:`SessionStore` does.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from clinical_copilot.db.models import DiscrepancyCacheRow
from clinical_copilot.logging import get_logger
from clinical_copilot.tools.records import FlagRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker

    from clinical_copilot.discrepancy.chart_provider import ChartProvider
    from clinical_copilot.discrepancy.engine import DiscrepancyEngine

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class _CachedFlags:
    flags: tuple[FlagRecord, ...]
    expires_at: datetime


class DiscrepancyCache:
    """In-process TTL + optional Postgres durable tier."""

    def __init__(
        self,
        *,
        chart_provider: ChartProvider,
        engine: DiscrepancyEngine,
        session_factory: sessionmaker[SqlSession] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._chart_provider = chart_provider
        self._engine = engine
        self._session_factory = session_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._memory: dict[str, _CachedFlags] = {}

    def get_flags(self, patient_id: str) -> list[FlagRecord]:
        """Return cached flags for ``patient_id``, recomputing on miss."""

        if not patient_id:
            raise ValueError("patient_id must be non-empty")

        now = self._clock()

        with self._lock:
            cached = self._memory.get(patient_id)
            if cached is not None and cached.expires_at > now:
                logger.debug(
                    "discrepancy_cache_hit_memory",
                    patient_id=patient_id,
                )
                return list(cached.flags)

        durable = self._read_durable(patient_id, now=now)
        if durable is not None:
            with self._lock:
                self._memory[patient_id] = durable
            logger.debug(
                "discrepancy_cache_hit_durable",
                patient_id=patient_id,
            )
            return list(durable.flags)

        logger.debug(
            "discrepancy_cache_miss",
            patient_id=patient_id,
        )
        return self._recompute_and_store(patient_id, now=now)

    def invalidate(self, patient_id: str) -> None:
        """Drop both tiers for ``patient_id``.

        Called by PR 15's write-path hooks (med save, lab post, allergy
        update, note sign). Idempotent — invalidating an absent entry is
        a no-op, never an error.
        """

        if not patient_id:
            raise ValueError("patient_id must be non-empty")

        with self._lock:
            self._memory.pop(patient_id, None)

        if self._session_factory is None:
            return

        session = self._session_factory()
        try:
            session.execute(
                delete(DiscrepancyCacheRow).where(
                    DiscrepancyCacheRow.patient_id == patient_id,
                ),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _recompute_and_store(self, patient_id: str, *, now: datetime) -> list[FlagRecord]:
        chart = self._chart_provider.load_chart(patient_id)
        flags = list(self._engine.evaluate(chart))
        expires_at = now + self._ttl

        with self._lock:
            self._memory[patient_id] = _CachedFlags(
                flags=tuple(flags),
                expires_at=expires_at,
            )

        self._write_durable(
            patient_id,
            flags=flags,
            computed_at=now,
            expires_at=expires_at,
        )
        return flags

    def _read_durable(
        self,
        patient_id: str,
        *,
        now: datetime,
    ) -> _CachedFlags | None:
        if self._session_factory is None:
            return None

        with self._session_factory() as session:
            row = session.scalar(
                select(DiscrepancyCacheRow).where(
                    DiscrepancyCacheRow.patient_id == patient_id,
                ),
            )
            if row is None:
                return None
            expires_at = _ensure_aware(row.expires_at)
            if expires_at <= now:
                return None
            flags = _decode_flags(row.flags_json)
            return _CachedFlags(flags=tuple(flags), expires_at=expires_at)

    def _write_durable(
        self,
        patient_id: str,
        *,
        flags: list[FlagRecord],
        computed_at: datetime,
        expires_at: datetime,
    ) -> None:
        if self._session_factory is None:
            return

        payload = _encode_flags(flags)
        session = self._session_factory()
        try:
            existing = session.scalar(
                select(DiscrepancyCacheRow).where(
                    DiscrepancyCacheRow.patient_id == patient_id,
                ),
            )
            if existing is None:
                session.add(
                    DiscrepancyCacheRow(
                        patient_id=patient_id,
                        flags_json=payload,
                        computed_at=computed_at,
                        expires_at=expires_at,
                    ),
                )
            else:
                existing.flags_json = payload
                existing.computed_at = computed_at
                existing.expires_at = expires_at
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _encode_flags(flags: list[FlagRecord]) -> str:
    return json.dumps([flag.model_dump(mode="json") for flag in flags])


def _decode_flags(payload: str) -> list[FlagRecord]:
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError("discrepancy_cache.flags_json must be a JSON array")
    return [FlagRecord.model_validate(item) for item in raw]


def _ensure_aware(value: datetime) -> datetime:
    """SQLite drops timezone info on round-trip; re-attach UTC.

    Postgres preserves ``timestamptz`` so this is a no-op there.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
