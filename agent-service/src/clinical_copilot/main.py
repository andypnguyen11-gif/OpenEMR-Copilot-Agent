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
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Path, Response, status
from pydantic import BaseModel, Field

from clinical_copilot import __version__
from clinical_copilot.app_state import AppState, build_app_state
from clinical_copilot.audit.log import AuditLogWriteError
from clinical_copilot.auth.jwt_verifier import require_clinician_claims
from clinical_copilot.config import Settings, get_settings
from clinical_copilot.logging import configure_logging, get_logger
from clinical_copilot.orchestrator.agent import UnknownLaneError
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.schemas import AgentResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from clinical_copilot.auth.session import ClinicianClaims


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

    return app


app = create_app()
