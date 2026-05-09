"""Pre-fetch a bounded patient chart pack for the supervisor.

The slow-lane supervisor's locked design has exactly two workers
(``intake_extractor``, ``evidence_retriever``) and exactly two
``tool_use`` schemas. Neither worker reads patient FHIR data, so chart
questions on the slow lane abstain. The chart pack closes that gap
*without* adding a third worker or a third tool schema:

1. Before ``supervisor.run()`` fires, the route handler asks
   :func:`build_chart_pack` to fetch a bounded slice of the bound
   patient's chart records (labs / meds / problems / allergies /
   visits / notes), dispatching through the existing
   :class:`PatientScopedToolRegistry` so the patient cross-check and
   audit-log writes are inherited unchanged.
2. The supervisor receives the resulting :class:`ChartPack` and
   prepends its :meth:`ChartPack.to_prompt_block` to the user message.
   Each line ends with a ``source_id`` (``ResourceType/{id}``) the
   model is instructed to copy verbatim into a :class:`CitedClaim`.
3. The response adapter widens its citation-anchor lookup to also
   accept any ``source_id`` present in the pack, so a chart-only
   answer (no worker call) still resolves to a grounded claim instead
   of NO_DATA.

Why a pre-fetch and not a worker
================================

The locked plan (``plans/week2-early-submission.md`` lines 7-21,
``plans/early-submission-supervisor-wiring.md`` lines 24-32) names two
workers and two tool schemas as load-bearing rubric items. Adding a
third would deviate. Pre-fetched context is request-time input — not a
worker — and the PRD explicitly allows tool-mediated chart access at
the supervisor boundary (PRD2 §A.5). The supervisor still dispatches
``evidence_retriever`` for guideline cites; the only thing the chart
pack does is make chart records available with stable source ids.

Patient isolation
=================

Every dispatch routes through
:meth:`PatientScopedToolRegistry.dispatch`, which:

* re-checks ``claims.patient_id`` against the bound scope and raises
  :class:`UnauthorizedToolCallError` on divergence
  (``tools/registry.py``);
* invokes :meth:`Tool.execute` which writes the audit row before any
  records leave the tool layer (``tools/base.py``).

This module deliberately does *not* re-implement either check. A
patient-mismatch wiring bug propagates up so the request fails closed.
Per-tool exceptions are swallowed into ``failed_topics`` so a single
flaky FHIR endpoint doesn't tank the whole pack — except authorization
errors, which propagate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from clinical_copilot.documents.schemas.citation import PatientChartCitation
from clinical_copilot.tools.base import (
    FhirAuthorizationDeniedError,
    UnauthorizedToolCallError,
)
from clinical_copilot.tools.records import (
    AllergyRecord,
    AnyRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
    VisitRecord,
)

if TYPE_CHECKING:
    from clinical_copilot.auth.session import ClinicianClaims
    from clinical_copilot.tools.records import ToolResult
    from clinical_copilot.tools.registry import PatientScopedToolRegistry


logger = structlog.get_logger(__name__)


ChartTopic = Literal["labs", "meds", "problems", "allergies", "visits", "notes"]


TOPIC_TO_TOOL: Final[dict[ChartTopic, str]] = {
    "labs": "get_labs",
    "meds": "get_meds",
    "problems": "get_problems",
    "allergies": "get_allergies",
    "visits": "get_visits",
    "notes": "get_notes",
}

# Display labels for the prompt block. The order here is the order
# topics appear in :meth:`ChartPack.to_prompt_block` so the LLM sees a
# stable, scannable layout (recent values first).
_TOPIC_LABEL: Final[dict[ChartTopic, str]] = {
    "labs": "Recent labs",
    "meds": "Active medications",
    "problems": "Active problems",
    "allergies": "Allergies",
    "visits": "Recent visits",
    "notes": "Recent notes",
}

DEFAULT_TOPICS: Final[tuple[ChartTopic, ...]] = (
    "labs",
    "meds",
    "problems",
    "allergies",
    "visits",
    "notes",
)

DEFAULT_PER_TOPIC_CAP: Final[int] = 5

# Per-topic record caps. Labs are intentionally roomier: a thyroid
# panel + iron studies + a CBC easily exceeds 5 observations on one
# visit, and the chat saw "no TSH on file" because 3 ferritin and
# 2 TPO antibody rows already filled the cap before the chart-pack
# reached the TSH rows. Other topics rarely need more than 5 to
# ground a clinical answer (active meds, problems, allergies are
# short lists).
_PER_TOPIC_CAPS: Final[Mapping[ChartTopic, int]] = {
    "labs": 10,
    "meds": DEFAULT_PER_TOPIC_CAP,
    "problems": DEFAULT_PER_TOPIC_CAP,
    "allergies": DEFAULT_PER_TOPIC_CAP,
    "visits": DEFAULT_PER_TOPIC_CAP,
    "notes": DEFAULT_PER_TOPIC_CAP,
}

# Lab observations are also date-filtered: anything older than this
# window is pruned even if it would fit under the cap. Six months
# captures a complete trend (e.g. quarterly A1c, monthly TSH on a
# titration patient) without dragging in stale values that confuse
# the synthesizer's "most recent X" answers.
_LABS_RECENT_WINDOW: Final[timedelta] = timedelta(days=180)

# Note bodies in the chart pack are clipped to keep the supervisor
# prompt token-bounded. ``_NOTE_PREVIEW_BODY_CAP`` is the inclusive
# raw-character cap; longer bodies are truncated to
# ``_NOTE_PREVIEW_TRUNCATE_AT`` chars and an ellipsis is appended so the
# prompt line remains readable.
_NOTE_PREVIEW_BODY_CAP: Final[int] = 200
_NOTE_PREVIEW_TRUNCATE_AT: Final[int] = 197


@dataclass(frozen=True, slots=True)
class ChartPackRecord:
    """One record from the chart pack with the fields the prompt needs.

    Carries the original Pydantic record alongside the prompt-side
    projection so the response adapter can rehydrate it into
    :class:`ToolResult`/:class:`AnyRecord` and surface full details
    (dose, observed_on, value+unit, etc.) on slow-lane cards. Without
    the original record the chat UI would only see source_id strings,
    not the structured per-record summary the fast lane shows.
    """

    source_id: str
    """``ResourceType/{id}`` shape, identical to the verification
    middleware's join key — the model is told to copy this into
    ``CitedClaim.source_id``."""

    resource_type: str
    """``Observation`` / ``MedicationRequest`` / etc. Set so the
    prompt header / debug logs can group by type without parsing the
    source_id string."""

    topic: ChartTopic
    """Which chart topic this record belongs to. Used to bucket
    records into the prompt block's sections AND to look up the
    tool_name (via :data:`TOPIC_TO_TOOL`) when the response adapter
    rebuilds :class:`ToolResult` objects for the wire payload."""

    summary: str
    """One-line human readable rendering. Shape varies per topic
    (``"TSH 6.73 mIU/L (observed_on=2026-04-05)"``,
    ``"levothyroxine 50 mcg PO daily (started=2026-04-10)"``)."""

    record: AnyRecord
    """The original Pydantic record returned by the tool. Held so the
    response adapter can pass it through as ``tool_results[*].records``
    — the chat UI's per-record summary renderer reads from it."""

    def to_citation(self) -> "PatientChartCitation":
        """Build the wire-shape :class:`PatientChartCitation` for this record.

        ``field_or_chunk_id`` mirrors :attr:`source_id` (already in
        ``ResourceType/{id}`` shape — the canonical FHIR reference
        syntax used by the discriminated-union contract). ``summary``
        is the one-line human-readable rendering — never the verbatim
        FHIR resource text, which would create a PHI redaction surface.
        """

        # source_id has shape "ResourceType/{id}" by chart-pack
        # producer contract; partition once so a malformed id (no
        # slash) still parses to a non-empty resource_id rather than
        # raising during a clinician-facing response build.
        _, _, resource_id = self.source_id.partition("/")
        return PatientChartCitation(
            field_or_chunk_id=self.source_id,
            resource_type=self.resource_type,
            resource_id=resource_id or self.source_id,
            display_summary=self.summary,
        )


@dataclass(frozen=True, slots=True)
class ChartPack:
    """Bounded chart slice the supervisor cites against.

    ``records`` is intentionally flat (not nested by topic) so the
    response-adapter's source_id lookup can return a
    ``frozenset[str]`` without rewalking the structure.
    """

    patient_id: str
    records: tuple[ChartPackRecord, ...]
    fetched_topics: tuple[ChartTopic, ...]
    failed_topics: tuple[ChartTopic, ...]

    def source_ids(self) -> frozenset[str]:
        return frozenset(record.source_id for record in self.records)

    def is_empty(self) -> bool:
        return not self.records

    def to_prompt_block(self) -> str:
        """Markdown the supervisor reads.

        Empty topics are skipped. A pack with no records returns an
        empty string so the caller can decide whether to inject
        anything at all.
        """

        if not self.records:
            return ""

        by_topic: dict[ChartTopic, list[ChartPackRecord]] = {}
        for record in self.records:
            by_topic.setdefault(record.topic, []).append(record)

        lines: list[str] = ["<patient_chart>"]
        for topic in DEFAULT_TOPICS:
            bucket = by_topic.get(topic)
            if not bucket:
                continue
            lines.append(f"## {_TOPIC_LABEL[topic]} ({len(bucket)} records)")
            for record in bucket:
                lines.append(f"- source_id={record.source_id} | {record.summary}")
        lines.append("</patient_chart>")
        return "\n".join(lines)


async def build_chart_pack(
    *,
    scoped_registry: PatientScopedToolRegistry,
    claims: ClinicianClaims,
    request_id: str,
    topics: Sequence[ChartTopic] = DEFAULT_TOPICS,
    per_topic_cap: int | Mapping[ChartTopic, int] | None = None,
) -> ChartPack:
    """Fan-out fetch the bound patient's recent chart records.

    Each topic dispatches through ``scoped_registry.dispatch`` so the
    patient cross-check and audit-log row are inherited from the same
    code path the v1 orchestrator uses. The dispatches are sync
    (``Tool.execute`` is sync; the AsyncBridge handles the FHIR await
    internally), so we offload them to the default thread pool and
    gather concurrently.

    Per-topic exceptions land in ``failed_topics`` and are logged.
    :class:`UnauthorizedToolCallError` and
    :class:`FhirAuthorizationDeniedError` propagate — a patient-scope
    or RBAC failure is a wiring bug we never want to hide behind a
    partially-populated pack.
    """

    log = logger.bind(
        request_id=request_id,
        patient_id=claims.patient_id,
    )

    loop = asyncio.get_running_loop()
    pending: list[ChartTopic] = []
    coros: list[asyncio.Future[ToolResult]] = []
    for topic in topics:
        tool_name = TOPIC_TO_TOOL.get(topic)
        if tool_name is None:
            # Unknown topic: log and skip. ChartTopic is a Literal, so
            # this only fires when a caller bypasses the type checker.
            log.warning("chart_pack.unknown_topic", topic=topic)
            continue
        coros.append(
            loop.run_in_executor(
                None,
                partial(
                    scoped_registry.dispatch,
                    tool_name,
                    claims=claims,
                    request_id=request_id,
                ),
            )
        )
        pending.append(topic)

    # ``return_exceptions=True`` collects every dispatch's result or
    # exception so we never leave an in-flight executor task with an
    # unread exception (which would log "Future exception was never
    # retrieved" warnings). After every dispatch resolves we walk the
    # outcomes — auth failures still propagate (after every result is
    # drained), per-topic failures still degrade.
    outcomes = await asyncio.gather(*coros, return_exceptions=True)

    fetched: list[ChartTopic] = []
    failed: list[ChartTopic] = []
    records: list[ChartPackRecord] = []
    auth_error: Exception | None = None

    for topic, outcome in zip(pending, outcomes, strict=True):
        if isinstance(outcome, (UnauthorizedToolCallError, FhirAuthorizationDeniedError)):
            # Authorization failures are never partial-success — a
            # mis-scoped registry or denied scope is a wiring bug, and
            # finishing the pack with the *other* topics would leak
            # whatever is in scope alongside a silently-missing
            # restricted topic. Defer the raise so every other future's
            # exception is observed first; once we have one, we'll
            # raise it at the end.
            if auth_error is None:
                auth_error = outcome
            continue
        if isinstance(outcome, BaseException):
            log.warning(
                "chart_pack.topic_failed",
                topic=topic,
                error=f"{type(outcome).__name__}: {outcome}",
            )
            failed.append(topic)
            continue

        topic_records = _project_tool_result(
            result=outcome,
            topic=topic,
            cap=_resolve_cap(per_topic_cap, topic),
        )
        if not topic_records:
            # No records is still a successful fetch. Treat empty as
            # fetched so the adapter knows we tried — distinct from a
            # tool that errored out.
            fetched.append(topic)
            continue
        records.extend(topic_records)
        fetched.append(topic)

    if auth_error is not None:
        raise auth_error

    log.info(
        "chart_pack.built",
        record_count=len(records),
        fetched_topics=fetched,
        failed_topics=failed,
    )
    return ChartPack(
        patient_id=claims.patient_id,
        records=tuple(records),
        fetched_topics=tuple(fetched),
        failed_topics=tuple(failed),
    )


def _resolve_cap(
    override: int | Mapping[ChartTopic, int] | None,
    topic: ChartTopic,
) -> int:
    """Resolve the per-topic record cap.

    ``None`` (the production default) reads :data:`_PER_TOPIC_CAPS`
    so labs get 10 and everything else gets 5. A bare ``int`` is the
    legacy contract — apply uniformly to every topic; useful in tests
    that want a deterministic cap regardless of topic. A mapping
    overrides the defaults per topic.
    """

    if override is None:
        return _PER_TOPIC_CAPS[topic]
    if isinstance(override, int):
        return override
    return override.get(topic, _PER_TOPIC_CAPS[topic])


def _project_tool_result(
    *,
    result: ToolResult,
    topic: ChartTopic,
    cap: int,
) -> list[ChartPackRecord]:
    """Collapse a :class:`ToolResult` into per-topic prompt records.

    The cap takes the *last* ``cap`` records on the assumption that
    each tool already orders its records most-recent-last (visits
    sort by visited_on, labs by observed_on, etc.). Reverse so the
    prompt shows newest first.

    Labs additionally get a recency window applied before the cap —
    a six-month sliding window per :data:`_LABS_RECENT_WINDOW`. This
    keeps lab trend questions ("what's her TSH been doing?") well-fed
    while preventing a high-volume patient's older observations from
    crowding out the recent panel members.
    """

    if cap <= 0:
        return []

    candidates = list(result.records)
    if topic == "labs":
        candidates = _filter_labs_to_window(candidates)

    # ``ToolResult.records`` is a Pydantic list; slicing produces a
    # fresh list, so reversing in place is safe.
    tail = candidates[-cap:]
    tail.reverse()

    projected: list[ChartPackRecord] = []
    for raw in tail:
        source_id = getattr(raw, "source_id", None)
        if not isinstance(source_id, str) or not source_id:
            continue
        resource_type, _, _ = source_id.partition("/")
        summary = _summarize(record=raw, topic=topic)
        projected.append(
            ChartPackRecord(
                source_id=source_id,
                resource_type=resource_type or "Unknown",
                topic=topic,
                summary=summary,
                record=raw,
            )
        )
    return projected


def _filter_labs_to_window(records: list[AnyRecord]) -> list[AnyRecord]:
    """Drop lab observations older than :data:`_LABS_RECENT_WINDOW`.

    Records with an unparsable ``observed_on`` pass through — better
    to surface a record we can't date than silently drop it. Non-lab
    records (defensive: this projector only sees labs when called for
    the labs topic) also pass through unchanged.
    """

    cutoff = datetime.now(UTC) - _LABS_RECENT_WINDOW
    kept: list[AnyRecord] = []
    for record in records:
        if not isinstance(record, LabRecord):
            kept.append(record)
            continue
        observed = _parse_observed_on(record.observed_on)
        if observed is None or observed >= cutoff:
            kept.append(record)
    return kept


def _parse_observed_on(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse for ``LabRecord.observed_on``.

    FHIR Observation.effectiveDateTime can be ``YYYY-MM-DD`` (date
    only), ``YYYY-MM-DDTHH:MM:SS`` (no tz), or with a timezone offset.
    ``datetime.fromisoformat`` handles all three on Python ≥ 3.11
    but rejects a trailing ``Z``; normalise that first. Returns
    ``None`` on any other shape so the caller can decide to keep the
    record rather than drop it.
    """

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Naive datetimes (no tz) — assume UTC so the cutoff comparison
    # is well-defined; FHIR observations from this stack are stored
    # as UTC anyway.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _summarize(*, record: AnyRecord, topic: ChartTopic) -> str:
    """One-line human-readable summary per record kind.

    ``topic`` is part of the signature so the call site doesn't have
    to recompute it, but dispatch happens off ``isinstance`` so each
    helper sees a fully-narrowed concrete type — no ``getattr`` games,
    no ``mypy`` union-attr complaints. The first matching record
    branch wins; the ``topic`` parameter is informational only.
    """

    del topic  # signature-stable; isinstance does the real narrowing
    for record_type, renderer in _RECORD_RENDERERS:
        if isinstance(record, record_type):
            return renderer(record)
    # Unreachable for any record kind chart_pack actually fetches —
    # the six chart topics map 1:1 to the six concrete classes
    # registered below. Keep a defensive fallback so an unknown
    # variant (FlagRecord — chart_pack does not enable get_flags)
    # doesn't crash the prompt build.
    return getattr(record, "source_id", "<unknown record>")


def _summarize_lab(record: LabRecord) -> str:
    unit = f" {record.unit}" if record.unit else ""
    return (
        f"{record.display}: {record.value}{unit} "
        f"(observed_on={record.observed_on})"
    )


def _summarize_med(record: MedicationRecord) -> str:
    dose = record.dose or "(dose unspecified)"
    started = record.started_on or "?"
    return f"{record.name} {dose} (started={started}, status={record.status})"


def _summarize_problem(record: ProblemRecord) -> str:
    onset = record.onset_date or "?"
    return f"{record.display} (onset={onset}, status={record.status})"


def _summarize_allergy(record: AllergyRecord) -> str:
    reaction = record.reaction or "(no reaction noted)"
    severity = record.severity or "(severity unspecified)"
    return f"{record.substance} - {reaction} - {severity}"


def _summarize_visit(record: VisitRecord) -> str:
    chief = record.chief_complaint or "(no CC)"
    return f"{record.encounter_type} on {record.visited_on} - {chief}"


def _summarize_note(record: NoteRecord) -> str:
    body = record.body
    if len(body) <= _NOTE_PREVIEW_BODY_CAP:
        preview = body
    else:
        preview = body[:_NOTE_PREVIEW_TRUNCATE_AT] + "..."
    return f"{record.note_date} ({record.author}): {preview}"


# Record-type -> renderer dispatch table. Pairs are walked in order
# in :func:`_summarize`, so the first ``isinstance`` match wins. The
# ``Callable[[Any], str]`` value type widens to absorb each helper's
# narrow record kind; the ``isinstance`` check guarantees we only
# call a renderer with a record it can handle.
_RECORD_RENDERERS: Final[tuple[tuple[type, Callable[[Any], str]], ...]] = (
    (LabRecord, _summarize_lab),
    (MedicationRecord, _summarize_med),
    (ProblemRecord, _summarize_problem),
    (AllergyRecord, _summarize_allergy),
    (VisitRecord, _summarize_visit),
    (NoteRecord, _summarize_note),
)
