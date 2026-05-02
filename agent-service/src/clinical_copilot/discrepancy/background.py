"""Background warm runner for the discrepancy cache (PR 15).

The cache itself already does read-through recompute on miss
(:meth:`DiscrepancyCache.get_flags`) and write-invalidate on demand
(:meth:`DiscrepancyCache.invalidate`). This module is the *trigger* —
given a panel of patient_ids, walk the list, call ``get_flags`` for
each, and report which ones succeeded.

Why not just inline this into the route handler? Two reasons:

1. The route handler should not own per-patient try/except. A single
   bad patient_id (one whose chart load throws) must not poison the
   whole panel — the warm endpoint promises best-effort, returns a
   summary, and lets the caller decide what to do with failures.
2. Keeping the runner separate from the route makes it trivially
   reusable from a future cron entry point or a CLI eval probe without
   spinning up FastAPI.

Concurrency note. The runner is intentionally sequential. Discrepancy
evaluation is sub-100ms per patient against the M5 fixtures (PR 13c
acceptance) and one chart load per patient against the live FHIR path,
so a 30-patient panel completes well under the slow-lane envelope. If
the production panel grows past where sequential warming becomes a
bottleneck, the cache itself would need single-flight machinery first
(see :class:`DiscrepancyCache`'s concurrency note); doing it here would
just race the duplicate-recompute window faster.

Failure semantics — the per-patient ``except`` is intentionally broad.
Anything the chart provider, engine, or durable-tier write can raise
(FHIR transport errors, ACL denials, malformed records, DB
connectivity) is reported as a failed entry without aborting the
panel. The reason string is the exception type name only; the message
may carry PHI from the upstream and we never want it on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from clinical_copilot.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from clinical_copilot.discrepancy.cache import DiscrepancyCache

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WarmFailure:
    """One row in :attr:`WarmSummary.failed`.

    ``reason`` is the exception's class name — never ``str(exc)`` and
    never ``repr(exc)`` because either could carry PHI bubbled up from a
    chart load. Operators get enough to file a ticket; the full trace
    sits in the structured logs (which redact via the same pipeline as
    the chat path, ARCHITECTURE §8.1).
    """

    patient_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WarmSummary:
    """Result of one :meth:`BackgroundRunner.warm_panel` call.

    ``warmed`` is the count of cache entries that now satisfy a
    subsequent ``get_flags`` from in-process or durable tier. ``failed``
    is the per-patient breakdown of why a recompute didn't happen —
    callers (and the warm route's response) carry the list verbatim so
    the gateway can decide whether to retry.
    """

    warmed: int
    failed: tuple[WarmFailure, ...]


class BackgroundRunner:
    """Thin wrapper that walks a patient panel through the cache.

    Construct one per app — the runner holds a reference to the same
    :class:`DiscrepancyCache` the ``get_flags`` tool reads through, so a
    warm here populates the same in-process tier the next chat request
    will hit.
    """

    def __init__(self, cache: DiscrepancyCache) -> None:
        self._cache = cache

    def warm_panel(self, patient_ids: Iterable[str]) -> WarmSummary:
        """Warm cache entries for every id in ``patient_ids``.

        Empty / blank ids in the panel are reported as failures (with
        reason ``"empty_patient_id"``) rather than skipped silently —
        the gateway is the source of truth for the panel and a blank id
        is a wiring bug worth surfacing.

        Duplicate ids are deduplicated up front because two warm calls
        for the same patient inside one panel would just race the
        duplicate-recompute window noted in :class:`DiscrepancyCache`'s
        docstring; nothing breaks but the work is wasted.
        """

        seen: set[str] = set()
        warmed = 0
        failures: list[WarmFailure] = []

        for patient_id in patient_ids:
            if not patient_id:
                failures.append(WarmFailure(patient_id="", reason="empty_patient_id"))
                continue
            if patient_id in seen:
                continue
            seen.add(patient_id)

            try:
                self._cache.get_flags(patient_id)
            except Exception as exc:
                # Reason is the type name only — see module docstring on
                # why the message is not safe to surface.
                reason = type(exc).__name__
                logger.warning(
                    "discrepancy_warm_failed",
                    patient_id=patient_id,
                    reason=reason,
                )
                failures.append(WarmFailure(patient_id=patient_id, reason=reason))
                continue

            warmed += 1

        return WarmSummary(warmed=warmed, failed=tuple(failures))
