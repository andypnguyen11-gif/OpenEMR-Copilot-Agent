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

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
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
from clinical_copilot.logging import configure_logging, get_logger
from clinical_copilot.observability.metrics import DEFAULT_WINDOW, MAX_WINDOW
from clinical_copilot.orchestrator.agent import UnknownLaneError
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.schemas import AgentResponse
from clinical_copilot.tools.records import FlagRecord

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
        try:
            return resolved_state.orchestrator.run(
                query=body.query,
                claims=claims,
                request_id=request_id,
                session_id=body.session_id,
                lane=body.lane,
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
            # unattributed PHI access from completing.
            get_logger(__name__).error(
                "agent_service.audit_write_failed",
                request_id=request_id,
                exception=str(exc),
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

    return app


app = create_app()
