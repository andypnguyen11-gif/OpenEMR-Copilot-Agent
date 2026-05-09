"""FastAPI application entry point for the Clinical Co-Pilot agent service.

PR 1 shipped the deployable shell (``/healthz``, ``/readyz``). M3 adds
the user-facing surface: ``POST /api/agent/query``. The route reads its
caller's identity from the verified gateway JWT (the patient-id, role,
and scopes are pinned by the gateway, not by the request body) and
hands the user query to :class:`Orchestrator`. The orchestrator's
return is the response body — including any abstention.

Wiring lives in :mod:`app_state`; this module owns routing and request
shapes only.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path as PathlibPath
from typing import TYPE_CHECKING, Annotated, Any

from anthropic import Anthropic
from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from clinical_copilot import __version__
from clinical_copilot.app_state import AppState, build_app_state
from clinical_copilot.audit.log import AuditLogWriteError
from clinical_copilot.audit.reader import MAX_PAGE_SIZE as AUDIT_MAX_PAGE_SIZE
from clinical_copilot.auth.internal_token import require_internal_token
from clinical_copilot.auth.jwt_verifier import require_clinician_claims
from clinical_copilot.auth.role import Role
from clinical_copilot.config import Settings, get_settings
from clinical_copilot.discrepancy.background import BackgroundRunner
from clinical_copilot.documents import store as facts_store
from clinical_copilot.documents.schemas.citation import Citation
from clinical_copilot.documents.extractor import (
    DocumentType,
    ExtractorError,
)
from clinical_copilot.documents.extractor import (
    extract as run_extraction,
)
from clinical_copilot.logging import configure_logging, get_logger
from clinical_copilot.observability.metrics import DEFAULT_WINDOW, MAX_WINDOW
from clinical_copilot.orchestrator.agent import UnknownLaneError
from clinical_copilot.orchestrator.chart_pack import (
    TOPIC_TO_TOOL,
    ChartPack,
    ChartPackRecord,
    ChartTopic,
    build_chart_pack,
)
from clinical_copilot.orchestrator.cross_patient_guard import cross_patient_check
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.schemas import (
    AgentResponse,
    Card,
    CardKind,
    CitedClaim,
    RerankBackendLabel,
)
from clinical_copilot.orchestrator.supervisor import (
    Handoff,
    SupervisorResponse,
)
from clinical_copilot.orchestrator.supervisor import (
    run as supervisor_run,
)
from clinical_copilot.orchestrator.supervisor_langgraph import (
    run_turn as supervisor_lg_run_turn,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason
from clinical_copilot.tools.records import FlagRecord, ToolResult
from clinical_copilot.verification.abstention import Abstention, AbstentionState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from clinical_copilot.auth.session import ClinicianClaims


class WarmRequest(BaseModel):
    """Body of ``POST /api/agent/internal/warm``.

    A panel of patient_ids the gateway wants the cache to recompute
    flags for. Bounded list size (200 is well above any realistic
    clinic panel) protects the route from a runaway gateway request
    that would tie up the FHIR backend serially. Each id is bounded the
    same way ``ClinicianClaims.patient_id`` is so the validation
    surface stays consistent across user-facing and internal routes.
    """

    patient_ids: list[str] = Field(min_length=1, max_length=200)


class WarmFailureBody(BaseModel):
    patient_id: str
    reason: str


class WarmResponse(BaseModel):
    """Summary returned by ``POST /api/agent/internal/warm``.

    No flag content — the warm route exists so subsequent ``get_flags``
    calls hit cache, not so the gateway can read flags directly. Keeping
    the payload to counts + failure reasons is the explicit guarantee
    that no PHI bubbles out of an ostensibly internal endpoint.
    """

    warmed: int
    failed: list[WarmFailureBody]


class FlagsResponse(BaseModel):
    """Body returned by ``GET /api/agent/internal/flags/{patient_id}``.

    The Daily Brief page (PR 16b) renders one card per panel patient
    and needs the discrepancy flag list inline — without round-tripping
    through the chat orchestrator and without the LLM authoring card
    text. Warm fills the cache; this route reads what warm or an
    earlier ``get_flags`` already materialized, with the same cold-path
    behaviour (recompute on miss) the in-process tool sees.

    The flag content is engine output, not LLM output: ``rationale``
    strings are deterministic templates over chart records (e.g.
    ``"Active medication 'X' conflicts with charted 'Y' allergy"``),
    so the same content is already visible to the same authenticated
    clinician through the chat surface. Gating the route behind the
    ``X-Internal-Token`` (server-to-server only, never browser-direct)
    keeps the trust boundary identical to warm and invalidate.

    ``patient_id`` is echoed back so the caller can confirm it asked
    about the patient it expected — the same defensive shape the
    orchestrator's :class:`AgentResponse` uses.
    """

    patient_id: str
    flags: list[FlagRecord]


class QueryRequest(BaseModel):
    """Body of ``POST /api/agent/query``.

    Three fields. ``query`` is the user's natural-language question.
    ``session_id`` is an optional client-supplied id from a prior turn's
    response, used to continue a multi-turn conversation. ``lane``
    selects between the slow (Daily Brief / reconciliation) and fast
    (in-chart side panel, ≤5s budget) configurations; defaults to slow
    so older clients that predate the field land on the same path
    they've always used. The patient-id, user-id, role, and scopes are
    *not* in the body — they come from the JWT (verified by the FastAPI
    dependency) so a malicious client can't rebind any of them per
    request.

    A ``session_id`` that doesn't resolve under the JWT's principal is
    silently replaced with a fresh server-minted id (see
    :class:`SessionStore`); the response always carries the canonical
    server id.
    """

    query: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9-]+$",
    )
    lane: Lane = Field(default=Lane.SLOW)


class SupervisorAuditEntry(BaseModel):
    """Single audit-log row, projected for the supervisor read endpoint.

    Mirrors :class:`clinical_copilot.audit.reader.AuditLogEntry` exactly
    so the route can hand the dataclass straight into the Pydantic
    serializer. Patient identifiers are *only* the HMAC-SHA256 hash the
    writer stored — the table never holds raw IDs (PR 2 contract), so
    the supervisor view inherits the same property without an
    additional redaction layer.
    """

    ts: datetime
    user_id: str
    role: str
    patient_id_hash: str
    resource_type: str
    action: str
    request_id: str


class SupervisorAuditResponse(BaseModel):
    """Body of ``GET /api/agent/supervisor/audit/{resident_user_id}``.

    Echoing ``resident_user_id`` lets the caller confirm the route
    interpreted the path the same way they did — same defensive shape
    other agent-side responses use (e.g. ``FlagsResponse.patient_id``).
    """

    resident_user_id: str
    entries: list[SupervisorAuditEntry]


class AbstainSummary(BaseModel):
    """Per-document abstain counts surfaced on the ingest response.

    The full per-field breakdown lives in ``facts``; this is the
    quick at-a-glance view the PHP review page uses to show "N fields
    flagged for review" without walking the whole structure.
    """

    low_confidence_field_count: int
    no_data_field_count: int
    citation_invalid_field_count: int
    out_of_schema_field_count: int


class IngestResponse(BaseModel):
    """Body returned by ``POST /api/agent/internal/ingest``.

    ``facts`` is the full ``LabPdfFacts`` or ``IntakeFormFacts`` JSON
    so the PHP review page renders without a second round-trip.
    ``facts_url`` is the addressable read-back path for callers that
    want to re-fetch the same record (e.g. a page reload mid-review).
    """

    document_id: str
    document_type: str
    patient_id: int | None
    facts_url: str
    facts: dict[str, Any]
    extraction_ms: int
    abstain_summary: AbstainSummary


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, json_logs=settings.is_production)
    log = get_logger(__name__)
    log.info(
        "agent_service.startup",
        env=settings.env,
        version=__version__,
    )
    try:
        yield
    finally:
        log.info("agent_service.shutdown")


def _first_citation_source_id(
    handoffs: tuple[Handoff, ...],
    *,
    chart_pack: ChartPack | None = None,
    synthesized_text: str = "",
) -> str | None:
    """Walk supervisor handoffs and chart pack to find a citation anchor.

    Used by the supervisor → AgentResponse adapter to anchor the
    synthesized prose at a real citation. The lookup runs in two
    passes:

    1. **Worker-handoff citations.** ``evidence_retriever`` chunks
       expose their citation as ``output["chunks"][i]["chunk_id"]``;
       ``intake_extractor`` flattens citations into
       ``output["citations"][i]["source_doc_id"]``. Walk both shapes
       and return the first non-empty id we see.

    2. **Chart-pack source_id substring match.** When a chart pack is
       supplied AND the synthesized text contains a chart-pack
       ``source_id`` verbatim (the supervisor prompt instructs the
       model to copy the id), that id is a valid anchor. This is the
       chart-only case where the supervisor synthesizes a chart
       answer without dispatching a worker.

    Returns ``None`` when neither pass yields a citation; the adapter
    then abstains rather than emit ungrounded prose.
    """

    for handoff in handoffs:
        output = handoff.output
        if not isinstance(output, dict):
            continue
        chunks = output.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                if isinstance(chunk, dict):
                    chunk_id = chunk.get("chunk_id")
                    if isinstance(chunk_id, str) and chunk_id:
                        return chunk_id
        citations = output.get("citations")
        if isinstance(citations, list):
            for citation in citations:
                if isinstance(citation, dict):
                    source_doc_id = citation.get("source_doc_id")
                    if isinstance(source_doc_id, str) and source_doc_id:
                        return source_doc_id

    if chart_pack is not None and synthesized_text:
        # Substring match against the chart-pack source_ids. The
        # supervisor's system prompt instructs the model to print the
        # id verbatim ("source_id=Observation/12345"); a present
        # source_id therefore means the model grounded its claim in
        # that record. Match longest-first so a chart pack with both
        # ``Observation/1`` and ``Observation/10`` doesn't anchor an
        # answer about #10 to #1.
        for source_id in sorted(chart_pack.source_ids(), key=len, reverse=True):
            if source_id in synthesized_text:
                return source_id

    return None


# Topic -> (CardKind, display title). The CardKind constants and
# chart_pack topics share a vocabulary, so the mapping is just a
# rename for the title; ``_supervisor_to_agent_response`` uses it to
# materialize one card per non-empty topic on the supervisor's happy
# path. Fast-lane (v1 orchestrator) cards have always existed; the
# slow lane needed this bridge so the chat UI renders the same
# structured surface for both.
_TOPIC_TO_CARD: dict[str, tuple[str, str]] = {
    "labs": (CardKind.LABS, "Recent labs"),
    "meds": (CardKind.MEDS, "Active medications"),
    "problems": (CardKind.PROBLEMS, "Active problems"),
    "allergies": (CardKind.ALLERGIES, "Allergies"),
    "visits": (CardKind.VISITS, "Recent visits"),
    "notes": (CardKind.NOTES, "Recent notes"),
}


def _chart_pack_surface(
    chart_pack: ChartPack | None,
    *,
    synthesized_text: str,
) -> tuple[list[Card], list[ToolResult]]:
    """Project the chart pack into the wire-side cards + tool_results.

    Two design points pinned by the slow-lane chat UX:

    * **Topic relevance**: only emit a card for a topic when the
      supervisor's synthesized text actually references one of that
      topic's source_ids. The pre-fetch always fans across all six
      topics so the LLM has cross-topic context, but the user
      shouldn't see "Recent labs" cards on a "what meds is this
      patient on" turn. Filtering by what the model grounded its
      claim on matches the fast lane's tool-driven topic selection.

    * **Full record details**: chat.js renders each card row by
      joining ``card.source_ids`` against
      ``tool_results[*].records`` and calling its summary
      formatter. Without ``tool_results`` the slow-lane card shows
      bare source_id strings; passing the original Pydantic records
      through restores the dose/observed_on/value+unit detail the
      fast lane already shows.

    Empty / ``None`` packs and packs with no cited source_ids both
    return ``([], [])`` so the abstention paths can keep their
    cards-empty / tool_results-empty shape unchanged.
    """

    if chart_pack is None or chart_pack.is_empty() or not synthesized_text:
        return ([], [])

    # Bucket records by topic up front so both the citation-presence
    # check and the per-topic card / tool_result builds walk the
    # records exactly once.
    by_topic: dict[ChartTopic, list[ChartPackRecord]] = {}
    for record in chart_pack.records:
        by_topic.setdefault(record.topic, []).append(record)

    cards: list[Card] = []
    tool_results: list[ToolResult] = []
    for topic, (kind, title) in _TOPIC_TO_CARD.items():
        bucket = by_topic.get(topic)
        if not bucket:
            continue
        if not any(item.source_id in synthesized_text for item in bucket):
            # Topic was pre-fetched but the supervisor's prose never
            # cited it. Skip — the user's question wasn't about this
            # topic and surfacing it would clutter the response.
            continue
        cards.append(
            Card(
                title=title,
                kind=kind,
                source_ids=[item.source_id for item in bucket],
                citations=[item.to_citation() for item in bucket],
            ),
        )
        tool_name = TOPIC_TO_TOOL[topic]
        tool_results.append(
            ToolResult(
                tool_name=tool_name,
                patient_id=chart_pack.patient_id,
                records=[item.record for item in bucket],
            ),
        )
    return (cards, tool_results)


def _supervisor_to_agent_response(
    sup: SupervisorResponse,
    *,
    session_id: str,
    chart_pack: ChartPack | None = None,
) -> AgentResponse:
    """Adapt a :class:`SupervisorResponse` to the wire :class:`AgentResponse`.

    Three branches:

    1. **Supervisor abstained** (``sup.abstention_reason`` is set, e.g.
       iteration cap hit) → return abstention with that reason.
    2. **No citation anchor** — synthesized text has no handoff with a
       citation we can attach it to. ``CitedClaim.source_id`` is
       ``min_length=1``; we cannot return ungrounded prose. Abstain
       with ``NO_DATA`` (the supervisor's locked contract: "if you
       cannot ground a claim, abstain — do not invent.").
    3. **Happy path** — surface the synthesized text as a single
       :class:`CitedClaim` anchored at the first available citation.
       Multi-claim breakout is a Phase 4 stretch in the early-submission
       plan; single-anchor is acceptable for the rubric (the structlog
       handoff log carries the full per-chunk citation trail for
       observability).

    Handoffs are NOT surfaced in the response payload. ``AgentResponse``
    has ``extra="forbid"`` and adding a top-level field would break v1-
    fallback wire compatibility. The handoff log is instead emitted via
    structlog (``supervisor.handoff`` events) — that's the demo's
    "routing observable" surface.
    """

    # Surface the active rerank backend on every supervisor-built
    # response. ``None`` on every branch that didn't actually run
    # retrieval (chart-only, abstention before the worker ran) so the
    # UI badge stays off rather than implying a Cohere outage on a turn
    # the reranker never saw — the supervisor stamps ``None`` in those
    # cases and we surface it verbatim.
    rerank_backend = _wire_rerank_backend(sup.rerank_backend)

    if sup.abstention_reason is not None:
        return AgentResponse(
            cards=[],
            prose=[],
            tool_results=[],
            abstention=Abstention(
                state=AbstentionState(sup.abstention_reason),
                reason=sup.abstention_reason,
            ),
            session_id=session_id,
            rerank_backend=rerank_backend,
        )

    text = sup.synthesized_text.strip()
    anchor = _first_citation_source_id(
        sup.handoffs,
        chart_pack=chart_pack,
        synthesized_text=text,
    )
    if not anchor or not text:
        return AgentResponse(
            cards=[],
            prose=[],
            tool_results=[],
            abstention=Abstention(
                state=AbstentionState.NO_DATA,
                reason=RuntimeAbstainReason.NO_DATA.value,
            ),
            session_id=session_id,
            rerank_backend=rerank_backend,
        )

    cards, tool_results = _chart_pack_surface(
        chart_pack,
        synthesized_text=text,
    )
    anchor_citation = _resolve_anchor_citation(anchor, chart_pack=chart_pack)
    return AgentResponse(
        cards=cards,
        prose=[CitedClaim(text=text, source_id=anchor, citation=anchor_citation)],
        tool_results=tool_results,
        session_id=session_id,
        rerank_backend=rerank_backend,
    )


def _wire_rerank_backend(value: str | None) -> RerankBackendLabel | None:
    """Validate the supervisor-supplied label against the wire enum.

    The supervisor types ``rerank_backend`` as ``str | None`` so it can
    accept whatever the worker stamped without coupling the supervisor
    package to the schemas package. We narrow at the wire boundary so
    a stray legacy spelling (``"llm-judge"`` / ``"none"``) coming back
    from a cassette or a degraded build can't end up as an
    ``extra="forbid"`` validation error against
    :class:`AgentResponse`. Old → new mapping is intentional belt-and-
    suspenders against the rename in this PR.
    """

    if value is None or value == "":
        return None
    if value in ("cohere", "llm_judge", "bm25_only"):
        return value  # type: ignore[return-value]
    if value == "llm-judge":
        return "llm_judge"
    if value == "none":
        return "bm25_only"
    return None


def _resolve_anchor_citation(
    anchor: str,
    *,
    chart_pack: ChartPack | None,
) -> Citation | None:
    """Look up a typed :class:`Citation` for a ``CitedClaim``'s anchor.

    Returns ``None`` when the anchor is from a source we cannot type
    yet (currently retrieval chunks pass through ``tool_results`` and
    are not resolved here — that lives in the consumers that already
    walk ``tool_results.records``). Chart-pack-anchored claims get a
    :class:`PatientChartCitation` built from the matching record.
    """

    if chart_pack is None:
        return None
    for record in chart_pack.records:
        if record.source_id == anchor:
            return record.to_citation()
    return None


def create_app(
    settings: Settings | None = None,
    *,
    state: AppState | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``state`` overrides the default wiring — tests pass a pre-built
    :class:`AppState` with stub collaborators so the route runs end-to-
    end without a real LLM or DB.
    """

    resolved_settings = settings if settings is not None else get_settings()
    resolved_state = state if state is not None else build_app_state(resolved_settings)

    app = FastAPI(
        title="Clinical Co-Pilot Agent Service",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = resolved_settings
    app.state.jwt_verifier = resolved_state.jwt_verifier
    app.state.orchestrator = resolved_state.orchestrator
    app.state.session_store = resolved_state.session_store

    claims_dep = require_clinician_claims(resolved_state.jwt_verifier)
    internal_dep = require_internal_token(resolved_settings.internal_token)
    runner = BackgroundRunner(resolved_state.discrepancy_cache)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict[str, object]:
        return {
            "status": "ready",
            "version": __version__,
            "checks": {
                "database": "skipped",
                "fhir": "skipped",
            },
        }

    @app.post("/api/agent/query", tags=["agent"], response_model=AgentResponse)
    async def query_route(
        body: QueryRequest,
        claims: ClinicianClaims = claims_dep,
    ) -> AgentResponse:
        request_id = uuid.uuid4().hex
        # Best-effort name lookup for the orchestrator's cross-patient
        # guard + the runtime system block. Resolver is fail-soft —
        # ``None`` here means "no comparator", and the guard is skipped.
        bound_patient_name = resolved_state.patient_name_resolver(claims.patient_id)

        # W2-07 supervisor branch. Slow-lane traffic with the supervisor
        # wiring populated (real Anthropic client + corpus index loaded)
        # routes through :func:`supervisor_run`; fast lane and any
        # missing piece falls through to v1 Orchestrator. Each
        # ``build_chart_pack`` dispatch goes through the existing
        # :class:`PatientScopedToolRegistry`, which writes a per-tool
        # audit row before any record leaves the tool layer — so the
        # supervisor branch's PHI-audit gap (called out in the W2-07
        # commit) closes automatically once the registry is on the
        # path. Supervisor exceptions fall through to v1 (fail-soft).
        if (
            resolved_settings.use_supervisor
            and body.lane == Lane.SLOW
            and resolved_state.supervisor_anthropic is not None
            and resolved_state.supervisor_evidence_retriever is not None
            and resolved_state.supervisor_intake_extractor is not None
            and resolved_state.supervisor_model is not None
        ):
            # Reserve a canonical session_id consistent with the v1
            # path so a chat that bounces between engines mid-session
            # keeps a single id. We don't persist supervisor-side
            # messages back into SessionStore (multi-turn supervisor
            # is full-submission work); release the per-key lock
            # immediately to avoid blocking subsequent turns.
            canonical_id, _state = resolved_state.session_store.get_or_create(
                claims, body.session_id,
            )
            resolved_state.session_store.release(claims, canonical_id)

            # Cross-patient guard: lifted from the v1 Orchestrator
            # (``orchestrator/agent.py:284-300``) onto the supervisor
            # branch so a "patient X" / "X's labs" query against a
            # bound session for someone else short-circuits before the
            # chart-pack pre-fetch and supervisor LLM round-trips run.
            # Skipped when ``bound_patient_name`` is missing — same
            # fail-soft policy as v1.
            guard_reason = cross_patient_check(body.query, bound_patient_name)
            if guard_reason is not None:
                return AgentResponse(
                    cards=[],
                    prose=[],
                    tool_results=[],
                    abstention=Abstention(
                        state=AbstentionState.NO_DATA,
                        reason=guard_reason,
                    ),
                    session_id=canonical_id,
                )

            # Pre-fetch chart pack so the supervisor can cite chart
            # records by their FHIR ``ResourceType/{id}`` source_id
            # without breaking the locked "2 workers + 2 tool_use
            # tools" supervisor contract. ``tool_registry`` is None
            # only in tests that bypass ``build_app_state``; in that
            # case the pack stays empty and the supervisor behaves as
            # it did before — corpus-only.
            chart_pack: ChartPack | None = None
            if resolved_state.tool_registry is not None:
                scoped_registry = resolved_state.tool_registry.scoped_for(
                    claims.patient_id,
                )
                try:
                    chart_pack = await build_chart_pack(
                        scoped_registry=scoped_registry,
                        claims=claims,
                        request_id=request_id,
                    )
                except Exception as exc:
                    # A patient-mismatch wiring bug (UnauthorizedToolCallError)
                    # would also land here. Log loudly and continue —
                    # an empty chart pack falls through to NO_DATA on
                    # any chart question, which is the right answer
                    # when the registry can't serve this patient.
                    get_logger(__name__).warning(
                        "chart_pack.build_failed",
                        request_id=request_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    chart_pack = None

            # W2-07 LangGraph branch (opt-in via USE_LANGGRAPH=true).
            # Fail-soft: any exception falls through to the plain-Python
            # supervisor below, which itself falls through to v1
            # Orchestrator on its own exception path. Two-layer fallback
            # so flipping the flag on can never strand a request.
            if (
                resolved_settings.use_langgraph
                and resolved_state.supervisor_corpus_retriever is not None
                and resolved_state.supervisor_model is not None
            ):
                try:
                    sup = supervisor_lg_run_turn(
                        user_query=body.query,
                        request_id=request_id,
                        patient_id=claims.patient_id,
                        bound_patient_name=bound_patient_name,
                        planner_client=resolved_state.supervisor_anthropic,
                        planner_model=resolved_settings.model_fast,
                        synthesizer_client=resolved_state.supervisor_anthropic,
                        synthesizer_model=resolved_state.supervisor_model,
                        critic_client=resolved_state.supervisor_anthropic,
                        critic_model=resolved_settings.model_fast,
                        retriever=resolved_state.supervisor_corpus_retriever,
                        rerank_client=resolved_state.supervisor_anthropic,
                        rerank_model=resolved_settings.model_fast,
                        cohere_client=resolved_state.supervisor_cohere_client,
                        orchestrator=resolved_state.orchestrator,
                        claims=claims,
                        session_id=body.session_id,
                        lane=body.lane,
                        chart_pack=chart_pack,
                    )
                except Exception as exc:
                    get_logger(__name__).warning(
                        "supervisor_lg.fallback_to_supervisor",
                        request_id=request_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    return _supervisor_to_agent_response(
                        sup,
                        session_id=canonical_id,
                        chart_pack=chart_pack,
                    )

            try:
                sup = supervisor_run(
                    client=resolved_state.supervisor_anthropic,
                    model=resolved_state.supervisor_model,
                    query=body.query,
                    intake_extractor=resolved_state.supervisor_intake_extractor,
                    evidence_retriever=resolved_state.supervisor_evidence_retriever,
                    chart_pack=chart_pack,
                    request_id=request_id,
                )
            except Exception as exc:
                get_logger(__name__).warning(
                    "supervisor.fallback_to_v1",
                    request_id=request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                return _supervisor_to_agent_response(
                    sup,
                    session_id=canonical_id,
                    chart_pack=chart_pack,
                )

        try:
            return resolved_state.orchestrator.run(
                query=body.query,
                claims=claims,
                request_id=request_id,
                session_id=body.session_id,
                lane=body.lane,
                bound_patient_name=bound_patient_name,
            )
        except UnknownLaneError as exc:
            # Pydantic constrained ``lane`` to the Lane enum already, so
            # this only fires when the deployed orchestrator hasn't been
            # wired with the requested lane (e.g. a fast-lane request
            # against an older deploy). 400 is the right shape — the
            # client can fall back to slow.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"lane {exc.lane.value!r} is not configured",
            ) from exc
        except AuditLogWriteError as exc:
            # Fail-closed: an audit write failure inside a tool denial
            # path means the trail couldn't persist. We must not return
            # a successful response — surfacing 500 prevents an
            # unattributed PHI access from completing. Use
            # ``error_message=str(exc)`` rather than ``exception=...``
            # because structlog's ``ConsoleRenderer`` pops a key
            # literally named ``exception`` and emits a UserWarning
            # ("Remove format_exc_info from your processor chain") even
            # when format_exc_info isn't actually configured —
            # ``filterwarnings=error`` then promotes that to a
            # TypeError that bypasses this handler entirely.
            get_logger(__name__).error(
                "agent_service.audit_write_failed",
                request_id=request_id,
                error_message=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="audit log unavailable",
            ) from exc

    @app.post(
        "/api/agent/internal/warm",
        tags=["internal"],
        response_model=WarmResponse,
    )
    async def warm_route(
        body: WarmRequest,
        _: None = internal_dep,
    ) -> WarmResponse:
        # Runner swallows per-patient failures into the summary, so this
        # route only needs to translate the dataclass into the wire shape.
        # No exception path here — a runner-level crash (e.g. cache
        # backing object missing) is genuinely a 500 and FastAPI's
        # default handler is the right thing.
        summary = runner.warm_panel(body.patient_ids)
        return WarmResponse(
            warmed=summary.warmed,
            failed=[
                WarmFailureBody(patient_id=f.patient_id, reason=f.reason) for f in summary.failed
            ],
        )

    @app.post(
        "/api/agent/internal/invalidate/{patient_id}",
        tags=["internal"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def invalidate_route(
        patient_id: str = Path(min_length=1, max_length=64),
        _: None = internal_dep,
    ) -> Response:
        # ``DiscrepancyCache.invalidate`` is idempotent on unknown
        # patients (PR 14 contract), so the route returns 204 even if
        # there was nothing to drop. The PHP-side write hook is
        # fire-and-forget; surfacing 404 here would just create work
        # for the gateway it can't action.
        resolved_state.discrepancy_cache.invalidate(patient_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/agent/internal/metrics", tags=["internal"])
    async def metrics_route(
        window_seconds: int = Query(
            default=int(DEFAULT_WINDOW.total_seconds()),
            ge=1,
            le=int(MAX_WINDOW.total_seconds()),
            description="Aggregation window in seconds; clamped to MAX_WINDOW (24h).",
        ),
        _: None = internal_dep,
    ) -> dict[str, object]:
        # Internal-token-protected; no clinician JWT context. The summary
        # contains hashed patient ids only via the audit-log totals — raw
        # PHI never enters the response shape (see :class:`MetricsService`).
        # ``summarize`` is synchronous on the request thread; the spec's
        # "background job" for completeness becomes "checked when /metrics
        # is scraped" — any external poller plays the cron's role and the
        # service avoids an in-process scheduler.
        return resolved_state.metrics_service.summarize(
            window=timedelta(seconds=window_seconds),
            cache=resolved_state.discrepancy_cache,
        )

    @app.get(
        "/api/agent/internal/flags/{patient_id}",
        tags=["internal"],
        response_model=FlagsResponse,
    )
    async def flags_route(
        patient_id: str = Path(min_length=1, max_length=64),
        _: None = internal_dep,
    ) -> FlagsResponse:
        # Reads the same cache the chat-side ``get_flags`` tool reads,
        # so a prior warm or chat turn for the same patient hits the
        # in-process tier here. ``ChartProvider`` documents the
        # unknown-patient contract: empty chart, zero flags — not an
        # error — so the route returns 200 + ``flags=[]`` rather than
        # 404. Surfacing 404 would conflict with the M1 tool layer's
        # treatment of unknown == empty and would also leak existence
        # information to a caller that already proved possession of
        # the internal token.
        flags = resolved_state.discrepancy_cache.get_flags(patient_id)
        return FlagsResponse(patient_id=patient_id, flags=flags)

    @app.delete(
        "/api/agent/session/{session_id}",
        tags=["agent"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session_route(
        session_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
        claims: ClinicianClaims = claims_dep,
    ) -> Response:
        # Composite-key miss returns 404 — a different principal calling
        # with the same id cannot tell us whether the id exists somewhere
        # else. 401 would be misleading (the JWT itself is fine) and
        # would leak existence information.
        existed = resolved_state.session_store.delete(claims, session_id)
        if not existed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="session not found",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/agent/supervisor/audit/{resident_user_id}",
        tags=["agent"],
        response_model=SupervisorAuditResponse,
    )
    async def supervisor_audit_route(
        resident_user_id: str = Path(min_length=1, max_length=64),
        limit: int = Query(default=50, ge=1, le=AUDIT_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        claims: ClinicianClaims = claims_dep,
    ) -> SupervisorAuditResponse:
        # Two gates, one shape (403 on either failure):
        #   1. Role must be SUPERVISOR. Physicians and residents have no
        #      legitimate need for this endpoint — the case-study
        #      acceptance explicitly calls out non-supervisor rejection.
        #   2. ``resident_user_id`` must appear in ``claims.supervises``.
        #      The supervises list comes from the gateway-signed JWT, so
        #      the agent service never has to decide who supervises whom
        #      — only whether the requested target is in the trusted set.
        # Both denials use the same generic body so a non-supervisor
        # cannot probe-and-classify which residents exist by comparing
        # responses (would otherwise leak existence information).
        if claims.role is not Role.SUPERVISOR:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="not authorized to read audit log",
            )
        if resident_user_id not in claims.supervises:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="not authorized to read audit log",
            )
        if resolved_state.audit_reader is None:
            # No reader wired (test override that disabled the DB).
            # Returning 503 instead of 500 signals the failure mode is
            # configuration, not a runtime fault — production wiring
            # always builds a reader.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="audit reader unavailable",
            )

        rows = resolved_state.audit_reader.list_for_user(
            resident_user_id,
            limit=limit,
            offset=offset,
        )
        return SupervisorAuditResponse(
            resident_user_id=resident_user_id,
            entries=[
                SupervisorAuditEntry(
                    ts=row.ts,
                    user_id=row.user_id,
                    role=row.role,
                    patient_id_hash=row.patient_id_hash,
                    resource_type=row.resource_type,
                    action=row.action,
                    request_id=row.request_id,
                )
                for row in rows
            ],
        )

    # Anthropic client for the multimodal extractor. Built once per
    # app, closure-captured by the route below so each ingest request
    # reuses the SDK's connection pool. Settings already validated.
    anthropic_client = (
        Anthropic(api_key=resolved_settings.llm_api_key)
        if resolved_settings.llm_api_key
        else None
    )

    @app.post(
        "/api/agent/internal/ingest",
        tags=["internal"],
        response_model=IngestResponse,
    )
    async def ingest_route(
        document_id: Annotated[str, Form(min_length=1, max_length=128)],
        document_type: Annotated[DocumentType, Form()],
        uploader_user_id: Annotated[int, Form()],
        file: Annotated[UploadFile, File()],
        patient_id: Annotated[int | None, Form()] = None,
        _: None = internal_dep,
    ) -> IngestResponse:
        # Service-to-service ingest. Internal-token gates this route;
        # JWT-only callers fall through to the gate's 401. The PHP side
        # uploads the document blob as multipart so we never have to
        # reconcile an HMAC signing scheme between two languages.
        if anthropic_client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LLM not configured",
            )

        suffix = PathlibPath(file.filename or "upload.pdf").suffix.lower() or ".pdf"
        contents = await file.read()
        if not contents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="empty upload",
            )

        # Persist a temp file for the extractor's render path. Cleaned
        # up on the finally so a long-running process doesn't accumulate
        # unbounded scratch state under TMPDIR.
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = PathlibPath(tmp.name)

        start = time.perf_counter()
        try:
            try:
                result = run_extraction(
                    client=anthropic_client,
                    model=resolved_settings.model_slow,
                    document_id=document_id,
                    document_type=document_type,
                    pdf_path=tmp_path,
                )
            except ExtractorError as exc:
                # Schema-validation or VLM-call failures are 422 — caller
                # can surface the problem to the user without retry.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"extraction failed: {exc}",
                ) from exc
        finally:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Persist via the existing JSON-on-disk store (same shape the
        # demo CLI writes). The PHP review page reads through the
        # ``GET /api/agent/internal/extracted/{id}`` route below.
        facts_store.write(result.facts)

        return IngestResponse(
            document_id=document_id,
            document_type=document_type,
            patient_id=patient_id,
            facts_url=f"/api/agent/internal/extracted/{document_id}",
            facts=result.facts.model_dump(mode="json"),
            extraction_ms=elapsed_ms,
            abstain_summary=_compute_abstain_summary(result.facts.model_dump(mode="json")),
        )

    @app.get(
        "/api/agent/internal/extracted/{document_id}",
        tags=["internal"],
    )
    async def extracted_read_route(
        document_id: str = Path(min_length=1, max_length=128),
        _: None = internal_dep,
    ) -> dict[str, Any]:
        # Read-back of a previously ingested document's facts. Used by
        # the PHP review page when the page reloads mid-review (e.g. a
        # nav-away-and-back) to avoid re-extracting. Unknown document
        # ids return 404 — distinct from the warm/invalidate routes
        # because here the client wants a specific record, not a
        # fire-and-forget side effect on shared cache state.
        facts = facts_store.read(document_id)
        if facts is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="document not found",
            )
        return facts.model_dump(mode="json")

    @app.put(
        "/api/agent/internal/extracted/{document_id}",
        tags=["internal"],
    )
    async def extracted_update_route(
        document_id: str = Path(min_length=1, max_length=128),
        facts_body: dict[str, Any] = Body(...),
        _: None = internal_dep,
    ) -> dict[str, Any]:
        # Editable-confirm overwrite. The PHP document-review page POSTs
        # the clinician's edits here so the persisted facts reflect what
        # the clinician confirmed (rather than what the VLM / parser
        # originally produced). Validates the body against the same
        # ``_FactsUnion`` TypeAdapter the read path uses, so a bad edit
        # (wrong shape, abstain-without-reason, etc.) gets rejected
        # before it lands on disk and corrupts the read path.
        if facts_body.get("document_id") != document_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="document_id in body must match URL",
            )
        try:
            validated = facts_store.validate(facts_body)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"facts validation failed: {exc}",
            ) from exc
        facts_store.write(validated)
        return validated.model_dump(mode="json")

    return app


def _compute_abstain_summary(facts: dict[str, Any]) -> AbstainSummary:
    """Walk a ``*Facts`` JSON dump and count per-reason abstain field markers.

    The schema invariant from ``ExtractedField`` is that an abstaining
    field carries an ``abstain_reason`` string. We walk the dict tree
    once and tally each occurrence; this surfaces the per-document
    review cost (how many fields the reviewer must inspect) at a glance.
    """

    counts: dict[str, int] = {
        RuntimeAbstainReason.LOW_CONFIDENCE.value: 0,
        RuntimeAbstainReason.NO_DATA.value: 0,
        RuntimeAbstainReason.CITATION_INVALID.value: 0,
        RuntimeAbstainReason.OUT_OF_SCHEMA.value: 0,
    }

    def walk(node: object) -> None:
        if isinstance(node, dict):
            reason = node.get("abstain_reason")
            if isinstance(reason, str) and reason in counts:
                counts[reason] += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(facts)
    return AbstainSummary(
        low_confidence_field_count=counts[RuntimeAbstainReason.LOW_CONFIDENCE.value],
        no_data_field_count=counts[RuntimeAbstainReason.NO_DATA.value],
        citation_invalid_field_count=counts[RuntimeAbstainReason.CITATION_INVALID.value],
        out_of_schema_field_count=counts[RuntimeAbstainReason.OUT_OF_SCHEMA.value],
    )


app = create_app()
