"""End-to-end tests for the citation-overlay page route.

The bbox-overlay UI on the OpenEMR review pages fetches each rendered
page through ``GET /api/agent/internal/document_page/{id}?page=N``.
What this test pins:

* the route reads from the same disk cache the ingest path writes to
  (no second source of truth, so the cache module's behavior carries
  through);
* a successful read returns ``image/png`` with the exact bytes that
  were written — the overlay JS measures intrinsic image dimensions
  to scale citation rectangles, so re-encoding here would silently
  break alignment;
* cache miss is the *route's* responsibility to surface with a body
  the PHP shell can render — it must be a 404 with a structured detail
  carrying ``reason``, ``document_id``, and ``page``, so PHP never has
  to guess whether the document, the page, or the renderer is at
  fault (the "PHP must not have to guess" guard from PR 5);
* the route distinguishes "document never rendered" from "page out of
  range" so the placeholder UI can be specific without re-reading the
  facts store;
* the internal-token gate matches the other ``/api/agent/internal/*``
  routes — a missing or wrong header is a 401, not a leaky 500.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.internal_token import INTERNAL_TOKEN_HEADER
from clinical_copilot.config import Settings
from clinical_copilot.documents import page_cache
from clinical_copilot.main import create_app
from clinical_copilot.tools.fixtures import FixtureStore

INTERNAL_TOKEN = "internal-" + ("x" * 32)
HMAC_SECRET = "x" * 64

# Minimum PNG header bytes — enough that ``image/png`` consumers don't
# fall over and that we can byte-compare round-trips.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class _SilentAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        hmac_secret=HMAC_SECRET,
        llm_api_key="test-not-used",
        fhir_base_url="http://localhost:0",
        database_url="sqlite:///:memory:",
        audit_salt="test-salt",
        oauth_client_id="cid",
        oauth_private_key_pem=b"",
        oauth_key_id="",
        oauth_token_url="http://localhost:0/token",
        model_slow="test-model-slow",
        model_fast="test-model-fast",
        internal_token=INTERNAL_TOKEN,
    )


@pytest.fixture
def cache_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    # Redirect the module-level default cache root for the duration of
    # the test so the suite never touches the developer's real
    # ``data/page_renders/`` tree.
    monkeypatch.setattr(page_cache, "_DEFAULT_CACHE_ROOT", tmp_path)
    yield tmp_path


@pytest.fixture
def client() -> TestClient:
    settings = _settings()
    state = build_app_state(
        settings,
        audit=_SilentAudit(),
        fixture_store=FixtureStore.from_file(),
    )
    app = create_app(settings, state=state)
    return TestClient(app)


def test_returns_cached_png_for_valid_document_and_page(
    cache_root: Path,
    client: TestClient,
) -> None:
    page_cache.write("doc-known", 1, _PNG_BYTES)

    response = client.get(
        "/api/agent/internal/document_page/doc-known",
        params={"page": 1},
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _PNG_BYTES


def test_unknown_document_returns_structured_404(
    cache_root: Path,
    client: TestClient,
) -> None:
    response = client.get(
        "/api/agent/internal/document_page/never-ingested",
        params={"page": 1},
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == {
        "reason": "document_not_rendered",
        "document_id": "never-ingested",
        "page": 1,
    }


def test_page_out_of_range_returns_structured_404(
    cache_root: Path,
    client: TestClient,
) -> None:
    page_cache.write("doc-two-pages", 1, _PNG_BYTES)
    page_cache.write("doc-two-pages", 2, _PNG_BYTES)

    response = client.get(
        "/api/agent/internal/document_page/doc-two-pages",
        params={"page": 5},
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == {
        "reason": "page_out_of_range",
        "document_id": "doc-two-pages",
        "page": 5,
        "cached_page_count": 2,
    }


def test_missing_internal_token_returns_401(
    cache_root: Path,
    client: TestClient,
) -> None:
    page_cache.write("doc-known", 1, _PNG_BYTES)

    response = client.get(
        "/api/agent/internal/document_page/doc-known",
        params={"page": 1},
    )

    assert response.status_code == 401


def test_wrong_internal_token_returns_401(
    cache_root: Path,
    client: TestClient,
) -> None:
    page_cache.write("doc-known", 1, _PNG_BYTES)

    response = client.get(
        "/api/agent/internal/document_page/doc-known",
        params={"page": 1},
        headers={INTERNAL_TOKEN_HEADER: "wrong-" + ("x" * 32)},
    )

    assert response.status_code == 401


def test_page_query_param_must_be_at_least_one(
    cache_root: Path,
    client: TestClient,
) -> None:
    response = client.get(
        "/api/agent/internal/document_page/doc-known",
        params={"page": 0},
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
    )

    # FastAPI surfaces ``ge=1`` violations as 422, not 400 — the route
    # never runs for invalid ranges.
    assert response.status_code == 422
