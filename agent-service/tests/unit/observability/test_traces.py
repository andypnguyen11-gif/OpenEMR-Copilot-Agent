"""Unit tests for the PR W2-04 traces module.

Cover the three contracts of :class:`TracesService`:

* the happy path actually persists a row with the new nullable columns;
* the fail-open contract: a DB exception logs and bumps the local
  counter, never propagates into the clinician path;
* the no-DB constructor (``session_factory=None``) is a logged no-op
  same as :class:`MetricsService` — used by the test paths that override
  ``audit`` and don't wire the durable tier.

The integration test
(``tests/integration/test_agent_query_writes_trace.py``) is the
cross-module pin that the orchestrator actually writes a row the trace
table can read back.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clinical_copilot.db.base import Base
from clinical_copilot.db.engine import create_session_factory
from clinical_copilot.db.models import AgentTrace
from clinical_copilot.main import (
    _compute_mean_confidence,
    _retrieval_hits_from_handoffs,
)
from clinical_copilot.observability.traces import (
    TraceRecord,
    TracesService,
    UsageTotals,
    read_failed_writes,
    reset_failed_writes,
)
from clinical_copilot.orchestrator.supervisor import Handoff, SupervisorResponse

if TYPE_CHECKING:
    pass


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


@pytest.fixture(autouse=True)
def _reset_failed_writes() -> Iterator[None]:
    reset_failed_writes()
    yield
    reset_failed_writes()


def _trace(**overrides: object) -> TraceRecord:
    base: dict[str, object] = {
        "request_id": "req-abc",
        "user_id": "dr-patel",
        "role": "physician",
        "lane": "slow",
        "latency_ms": 210,
        "token_in": 512,
        "token_out": 128,
        "model_tier": "claude-sonnet-test",
        "retrieval_hits": 4,
        "extraction_confidence": None,
    }
    base.update(overrides)
    return TraceRecord(**base)  # type: ignore[arg-type]


class TestUsageTotals:
    def test_default_is_zero(self) -> None:
        usage = UsageTotals()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_addition_is_componentwise(self) -> None:
        a = UsageTotals(input_tokens=10, output_tokens=3)
        b = UsageTotals(input_tokens=4, output_tokens=11)
        merged = a + b
        assert merged == UsageTotals(input_tokens=14, output_tokens=14)

    def test_addition_returns_new_instance(self) -> None:
        a = UsageTotals(input_tokens=10, output_tokens=3)
        b = UsageTotals(input_tokens=4, output_tokens=11)
        _ = a + b
        # Frozen dataclasses don't allow mutation; this test pins that
        # the operation is a pure ``+`` (caller's totals untouched).
        assert a == UsageTotals(input_tokens=10, output_tokens=3)
        assert b == UsageTotals(input_tokens=4, output_tokens=11)


class TestTracesServiceRecord:
    def test_persists_row_with_all_columns(
        self, session_factory: sessionmaker[Session], engine: Engine,
    ) -> None:
        traces = TracesService(session_factory=session_factory)

        traces.record(_trace())

        with Session(engine) as session:
            row = session.execute(select(AgentTrace)).scalar_one()
        assert row.request_id == "req-abc"
        assert row.user_id == "dr-patel"
        assert row.role == "physician"
        assert row.lane == "slow"
        assert row.latency_ms == 210
        assert row.token_in == 512
        assert row.token_out == 128
        assert row.model_tier == "claude-sonnet-test"
        assert row.retrieval_hits == 4
        assert row.extraction_confidence is None

    def test_extraction_confidence_only_row_keeps_retrieval_hits_null(
        self, session_factory: sessionmaker[Session], engine: Engine,
    ) -> None:
        # Document-ingest shape: extraction_confidence populated,
        # retrieval_hits stays NULL. NULL semantics are independent
        # per column.
        traces = TracesService(session_factory=session_factory)

        traces.record(
            _trace(
                request_id="req-ingest",
                lane="ingest",
                retrieval_hits=None,
                extraction_confidence=0.87,
            ),
        )

        with Session(engine) as session:
            row = session.execute(
                select(AgentTrace).where(AgentTrace.request_id == "req-ingest"),
            ).scalar_one()
        assert row.retrieval_hits is None
        assert row.extraction_confidence == pytest.approx(0.87)

    def test_retrieval_hits_only_row_keeps_extraction_confidence_null(
        self, session_factory: sessionmaker[Session], engine: Engine,
    ) -> None:
        # Slow-lane retrieval-only shape: retrieval_hits populated,
        # extraction_confidence stays NULL.
        traces = TracesService(session_factory=session_factory)

        traces.record(_trace(request_id="req-slow", retrieval_hits=7, extraction_confidence=None))

        with Session(engine) as session:
            row = session.execute(
                select(AgentTrace).where(AgentTrace.request_id == "req-slow"),
            ).scalar_one()
        assert row.retrieval_hits == 7
        assert row.extraction_confidence is None


class TestTracesServiceFailOpen:
    def test_db_exception_does_not_propagate(self) -> None:
        # Simulate a DB outage: the session's commit raises. The writer
        # must log + bump the local counter, never re-raise — otherwise
        # an observability hiccup becomes a clinician-facing 5xx.
        session = MagicMock(spec=Session)
        session.commit.side_effect = OperationalError("stmt", {}, Exception("db down"))
        factory = MagicMock(return_value=session)
        traces = TracesService(session_factory=factory)

        # Should not raise.
        traces.record(_trace())

        assert read_failed_writes() == 1
        session.rollback.assert_called_once()
        session.close.assert_called_once()

    def test_no_session_factory_is_logged_noop(self) -> None:
        # Test paths that override ``audit`` end up with
        # ``session_factory=None``. The writer must not attempt a DB
        # round-trip and must not bump the counter (this isn't a
        # failure — it's the documented test-mode contract).
        traces = TracesService(session_factory=None)

        traces.record(_trace())

        assert read_failed_writes() == 0


def _retriever_handoff(*, chunks: int) -> Handoff:
    return Handoff(
        worker="evidence_retriever",
        tool_use_id="tu",
        arguments={},
        output={"chunks": [{"chunk_id": f"c{i}"} for i in range(chunks)]},
        error=None,
        latency_ms=12,
    )


def _langgraph_retriever_handoff(*, citations: int) -> Handoff:
    """Mimic the LangGraph adapter's projected handoff shape.

    ``_state_handoff_to_dataclass`` in supervisor_langgraph.py projects
    only ``output["citations"]`` (the worker-level chunks list is
    discarded). A reader that walks ``output["chunks"]`` only would
    silently NULL ``retrieval_hits`` on every LangGraph turn even when
    retrieval ran — this fixture pins that shape.
    """

    return Handoff(
        worker="evidence_retriever",
        tool_use_id="",
        arguments={},
        output={"citations": [{"chunk_id": f"c{i}"} for i in range(citations)]},
        error=None,
        latency_ms=0,
    )


def _intake_handoff() -> Handoff:
    return Handoff(
        worker="intake_extractor",
        tool_use_id="tu-i",
        arguments={},
        output={"document_id": "abc"},
        error=None,
        latency_ms=33,
    )


class TestRetrievalHitsFromHandoffs:
    def test_none_when_no_evidence_retriever_handoff_fired(self) -> None:
        sup = SupervisorResponse(synthesized_text="", handoffs=())
        assert _retrieval_hits_from_handoffs(sup) is None

    def test_none_when_only_non_retriever_handoffs(self) -> None:
        # Chart-only / document-only turn — no retriever ever ran. The
        # column must be NULL, NOT 0, so the dashboard can distinguish
        # "retriever didn't run" from "retriever ran and got 0 hits".
        sup = SupervisorResponse(
            synthesized_text="",
            handoffs=(_intake_handoff(),),
        )
        assert _retrieval_hits_from_handoffs(sup) is None

    def test_sums_chunks_across_evidence_retriever_handoffs(self) -> None:
        sup = SupervisorResponse(
            synthesized_text="",
            handoffs=(
                _retriever_handoff(chunks=3),
                _intake_handoff(),  # ignored
                _retriever_handoff(chunks=5),
            ),
        )
        assert _retrieval_hits_from_handoffs(sup) == 8

    def test_zero_chunks_reads_as_zero_not_none(self) -> None:
        # A retriever that ran but matched nothing in the corpus.
        # Distinct from the "didn't run" case above — preserves the
        # NULL-vs-0 distinction.
        sup = SupervisorResponse(
            synthesized_text="",
            handoffs=(_retriever_handoff(chunks=0),),
        )
        assert _retrieval_hits_from_handoffs(sup) == 0

    def test_falls_back_to_citations_when_chunks_absent(self) -> None:
        # Regression pin: the LangGraph adapter projects only
        # ``output["citations"]`` (no ``chunks`` key). Reading only
        # ``chunks`` would NULL ``retrieval_hits`` on every LangGraph
        # turn even when retrieval ran. Bug 2026-05-09 — caught by
        # browser smoke against the live supervisor.
        sup = SupervisorResponse(
            synthesized_text="",
            handoffs=(_langgraph_retriever_handoff(citations=5),),
        )
        assert _retrieval_hits_from_handoffs(sup) == 5

    def test_chunks_take_precedence_over_citations(self) -> None:
        # Plain-Python supervisor handoffs carry both keys (chunks is
        # the worker-level full list; citations is a metadata subset).
        # Read chunks first to keep the count meaningful — citations is
        # only the fallback for handoff shapes that lack chunks.
        sup = SupervisorResponse(
            synthesized_text="",
            handoffs=(
                Handoff(
                    worker="evidence_retriever",
                    tool_use_id="tu",
                    arguments={},
                    output={
                        "chunks": [{"chunk_id": "c0"}, {"chunk_id": "c1"}],
                        "citations": [{"chunk_id": "c0"}],
                    },
                    error=None,
                    latency_ms=10,
                ),
            ),
        )
        assert _retrieval_hits_from_handoffs(sup) == 2


class TestComputeMeanConfidence:
    def test_none_when_no_confidences_present(self) -> None:
        # Distinct from a real ``0.0`` — the column lands NULL when the
        # extractor produced no confidence signal at all.
        assert _compute_mean_confidence({"foo": "bar", "nested": {"baz": 1}}) is None

    def test_walks_nested_dicts_and_lists(self) -> None:
        facts = {
            "observations": [
                {"value": "a", "confidence": 0.8},
                {"value": "b", "confidence": 0.6},
            ],
            "patient": {
                "name": "x",
                "dob_confidence": "not numeric, ignored",  # not a 'confidence' key
                "address": {"confidence": 1.0},
            },
        }
        assert _compute_mean_confidence(facts) == pytest.approx((0.8 + 0.6 + 1.0) / 3)

    def test_ignores_non_numeric_confidence_values(self) -> None:
        # An ``ExtractedField`` that abstained may carry
        # ``confidence: None``. The mean walker must skip those rather
        # than coercing to 0 (which would drag the average down).
        facts = {
            "field_a": {"confidence": 0.9},
            "field_b": {"confidence": None},
            "field_c": {"confidence": "low"},
        }
        assert _compute_mean_confidence(facts) == pytest.approx(0.9)
