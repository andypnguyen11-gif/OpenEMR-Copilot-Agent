"""End-to-end test for the PR W2-04 ``agent_traces`` writer on the
document-ingest path.

Sibling of ``test_agent_query_writes_trace.py`` — different shape of
trace row. The ingest entry point produces:

* ``extraction_confidence`` populated (mean per-field confidence from
  the document extractor's facts dump);
* ``retrieval_hits`` ``NULL`` (extraction never invokes the corpus
  retriever).

The cross-module pin: the ingest route mounts on ``create_app``, the
``TracesService`` constructed by ``build_app_state`` is the same one
the route writes to, and the row's columns reflect the independent-
nullable contract on the migration.

We monkeypatch ``run_extraction`` rather than calling the real VLM —
the existing ingest tests don't mock Anthropic either (they cover the
401 / 400 paths), and a real extractor call would require a fixture
PDF + a network round-trip. Stubbing the extractor lets us assert the
trace-writer wiring in isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clinical_copilot.app_state import build_app_state
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.reader import AuditLogReader
from clinical_copilot.auth.internal_token import INTERNAL_TOKEN_HEADER
from clinical_copilot.config import Settings
from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.db.models import AgentTrace
from clinical_copilot.documents.extractor import ExtractionResult
from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.lab_pdf import LabObservation, LabPdfFacts
from clinical_copilot.main import create_app
from clinical_copilot.observability.traces import TracesService
from clinical_copilot.tools.fixtures import FixtureStore
from datetime import date

INTERNAL_TOKEN = "internal-" + ("x" * 32)
HMAC_SECRET = "x" * 64


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


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


def _build_client(session_factory: sessionmaker[Session]) -> TestClient:
    settings = _settings()
    audit_writer = AuditLogWriter(session_factory=session_factory)
    audit_reader = AuditLogReader(session_factory=session_factory)
    state = build_app_state(
        settings,
        audit=audit_writer,
        audit_reader=audit_reader,
        fixture_store=FixtureStore.from_file(),
    )
    test_traces = TracesService(session_factory=session_factory)
    object.__setattr__(state, "traces_service", test_traces)
    object.__setattr__(state.orchestrator, "_traces", test_traces)

    app = create_app(settings, state=state)
    return TestClient(app)


def _cite(*, confidence: float, path: str) -> SourceCitation:
    return SourceCitation(
        document_id="doc-stub",
        page=1,
        bbox=(0.0, 0.0, 1.0, 1.0),
        confidence=confidence,
        raw_text="x",
        field_or_chunk_id=path,
    )


def _stub_extraction_result(*, citation_confidence: float) -> ExtractionResult:
    """Build a real :class:`ExtractionResult` whose dump exposes known
    ``confidence`` values that :func:`_compute_mean_confidence` will
    average. Every ``SourceCitation`` on a populated field carries the
    same confidence so the mean is exactly that value — keeps the test
    pinned to the writer's behavior, not the walker's averaging math.
    """

    obs = LabObservation(
        code=ExtractedField[str](
            value="2345-7",
            citation=_cite(confidence=citation_confidence, path="observations[0].code"),
        ),
        display=ExtractedField[str](
            value="Glucose",
            citation=_cite(confidence=citation_confidence, path="observations[0].display"),
        ),
        value=ExtractedField[float](
            value=142.0,
            citation=_cite(confidence=citation_confidence, path="observations[0].value"),
        ),
        unit=ExtractedField[str](
            value="mg/dL",
            citation=_cite(confidence=citation_confidence, path="observations[0].unit"),
        ),
        effective_date=ExtractedField[date](
            value=date(2025, 11, 12),
            citation=_cite(confidence=citation_confidence, path="observations[0].effective_date"),
        ),
    )
    facts = LabPdfFacts(document_id="doc-stub", observations=[obs])
    return ExtractionResult(
        document_id="doc-stub",
        document_type="lab_pdf",
        facts=facts,
        raw_tool_input={"document_path": "stub.pdf"},
    )


def test_document_ingest_writes_one_trace_row(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the extractor so we don't hit Anthropic. The trace writer
    # is what's under test; the extractor's real behavior is covered
    # by the per-extractor unit tests.
    citation_confidence = 0.85
    stub_result = _stub_extraction_result(citation_confidence=citation_confidence)

    def _fake_run_extraction(**_kwargs: object) -> ExtractionResult:
        return stub_result

    import clinical_copilot.main as main_module

    monkeypatch.setattr(main_module, "run_extraction", _fake_run_extraction)

    client = _build_client(session_factory)

    response = client.post(
        "/api/agent/internal/ingest",
        headers={INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN},
        data={
            "document_id": "doc-stub",
            "document_type": "lab_pdf",
            "uploader_user_id": "42",
        },
        files={"file": ("doc.pdf", b"%PDF-1.4\nstub\n", "application/pdf")},
    )

    assert response.status_code == 200, response.text

    with Session(session_factory.kw["bind"]) as session:
        rows = session.execute(select(AgentTrace)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "42"
    assert row.role == "internal"
    assert row.lane == "ingest"
    assert row.model_tier == "test-model-slow"
    # Document-ingest shape: extraction_confidence populated, retrieval
    # untouched. Independent nullables per the 0004 migration's contract.
    assert row.retrieval_hits is None
    assert row.extraction_confidence == pytest.approx(citation_confidence)
    # Latency is measured by the ingest route's perf_counter window.
    assert row.latency_ms >= 0
