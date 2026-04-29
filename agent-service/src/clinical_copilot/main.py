"""FastAPI application entry point for the Clinical Co-Pilot agent service.

This is the deployable shell from TASKS.md PR 1: just ``/healthz`` (liveness)
and ``/readyz`` (readiness). Real auth, tools, orchestrator, and verification
land in later PRs.

The split between liveness and readiness matters for Railway:
``/healthz`` answers "is the process alive?" and must never depend on
downstream services. ``/readyz`` answers "is this instance willing to take
traffic?" and is allowed to fail when a dependency the process needs is
unhealthy. PR 1 has no real dependencies, so readiness is a stub that
reports the structural fields a future check will fill in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from clinical_copilot import __version__
from clinical_copilot.config import Settings, get_settings
from clinical_copilot.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="Clinical Co-Pilot Agent Service",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = resolved

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

    return app


app = create_app()
