"""Pin the failure-mode contract of the internal-token FastAPI dep.

The dep is small enough that the only behaviour worth pinning is the
401-on-missing / 401-on-mismatch / 200-on-match triad plus the wiring
guard against an empty expected secret. Higher-level routing tests in
``test_internal_routes.py`` exercise the dep through real routes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clinical_copilot.auth.internal_token import (
    INTERNAL_TOKEN_HEADER,
    require_internal_token,
)

_SECRET = "x" * 32


def _app() -> FastAPI:
    app = FastAPI()
    dep = require_internal_token(_SECRET)

    @app.post("/protected")
    def protected(_: None = dep) -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_missing_header_returns_401() -> None:
    response = TestClient(_app()).post("/protected")
    assert response.status_code == 401


def test_mismatched_token_returns_401() -> None:
    response = TestClient(_app()).post(
        "/protected",
        headers={INTERNAL_TOKEN_HEADER: "y" * 32},
    )
    assert response.status_code == 401


def test_length_mismatch_returns_401() -> None:
    # Length-mismatch path runs before compare_digest — confirm it still
    # rejects rather than crashing on the constant-time comparison.
    response = TestClient(_app()).post(
        "/protected",
        headers={INTERNAL_TOKEN_HEADER: "x"},
    )
    assert response.status_code == 401


def test_matching_token_passes() -> None:
    response = TestClient(_app()).post(
        "/protected",
        headers={INTERNAL_TOKEN_HEADER: _SECRET},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_authorization_header_does_not_satisfy_gate() -> None:
    # The dep deliberately reads X-Internal-Token, not Authorization, so
    # a PHP client mis-defaulting to bearer-token auth cannot pass.
    response = TestClient(_app()).post(
        "/protected",
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    assert response.status_code == 401


def test_empty_expected_secret_is_a_wiring_error() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        require_internal_token("")
