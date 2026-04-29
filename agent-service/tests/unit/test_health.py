"""Health-endpoint tests for PR 1 — the deployable shell.

These are the only assertions that gate Milestone 0: the process boots, the
ASGI app exposes both probes, and each returns 200 with the documented
schema. Anything richer (DB ping, FHIR reachability, JWT) lands in later PRs
and gets its own targeted tests.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from clinical_copilot import __version__
from clinical_copilot.main import create_app


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "version": __version__}


def test_readyz_returns_ready_with_check_skeleton() -> None:
    client = TestClient(create_app())

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["version"] == __version__
    assert set(body["checks"].keys()) == {"database", "fhir"}


def test_unknown_route_is_404() -> None:
    client = TestClient(create_app())

    response = client.get("/does-not-exist")

    assert response.status_code == 404
