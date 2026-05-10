"""Unit tests for ``_stage_latencies_from_handoffs`` in :mod:`main`.

The helper aggregates per-stage timings from a supervisor turn so the
slow-lane query route can stamp ``AgentResponse.stage_latencies_ms``.
The contract is finicky enough to deserve its own test: the
``supervisor_dispatch`` key sums every handoff's worker round-trip,
``retriever``/``rerank`` are conditionally present (only when at least
one evidence_retriever handoff actually populated them), and ``total``
mirrors the request-level wall-clock the route already computes.
"""

from __future__ import annotations

from clinical_copilot.main import _stage_latencies_from_handoffs
from clinical_copilot.observability.traces import UsageTotals
from clinical_copilot.orchestrator.supervisor import Handoff, SupervisorResponse


def _sup(handoffs: tuple[Handoff, ...]) -> SupervisorResponse:
    return SupervisorResponse(
        synthesized_text="ok",
        handoffs=handoffs,
        iterations=1,
        usage_totals=UsageTotals(),
    )


def test_no_handoffs_yields_only_total() -> None:
    """An abstention before any worker dispatched produces no
    per-worker breakdown — only the total stays meaningful."""

    stages = _stage_latencies_from_handoffs(_sup(()), total_ms=42)
    assert stages == {"total": 42}


def test_intake_only_records_supervisor_dispatch_no_retriever() -> None:
    """An intake-only turn has no retriever stage — the helper must
    not emit a phantom ``retriever`` key with a zero value, since
    "stage didn't run" and "stage took zero time" are different
    facts a trace reader will draw inferences from."""

    handoff = Handoff(
        worker="intake_extractor",
        tool_use_id="tu-1",
        arguments={},
        output={"facts": []},
        error=None,
        latency_ms=180,
    )
    stages = _stage_latencies_from_handoffs(_sup((handoff,)), total_ms=300)
    assert stages == {"total": 300, "supervisor_dispatch": 180}


def test_evidence_retriever_handoff_propagates_retriever_and_rerank() -> None:
    """An evidence_retriever turn surfaces the per-stage timings the
    worker stamped on its tool-result payload (see
    :class:`EvidenceRetrieverOutput.to_tool_result`)."""

    handoff = Handoff(
        worker="evidence_retriever",
        tool_use_id="tu-2",
        arguments={},
        output={
            "chunks": [],
            "rerank_backend": "cohere",
            "retriever_ms": 35,
            "rerank_ms": 80,
        },
        error=None,
        latency_ms=140,
    )
    stages = _stage_latencies_from_handoffs(_sup((handoff,)), total_ms=400)
    assert stages == {
        "total": 400,
        "supervisor_dispatch": 140,
        "retriever": 35,
        "rerank": 80,
    }


def test_evidence_retriever_with_zero_rerank_omits_rerank_key() -> None:
    """A BM25-only fallback (rerank stage didn't run, so ``rerank_ms``
    is 0) must not emit a misleading ``rerank: 0`` key. The retriever
    stage stays present because BM25 is the retriever."""

    handoff = Handoff(
        worker="evidence_retriever",
        tool_use_id="tu-3",
        arguments={},
        output={
            "chunks": [],
            "rerank_backend": "bm25_only",
            "retriever_ms": 12,
            "rerank_ms": 0,
        },
        error=None,
        latency_ms=18,
    )
    stages = _stage_latencies_from_handoffs(_sup((handoff,)), total_ms=120)
    assert stages == {
        "total": 120,
        "supervisor_dispatch": 18,
        "retriever": 12,
    }


def test_two_workers_sums_supervisor_dispatch_and_retriever_rerank() -> None:
    """A multi-handoff turn folds dispatch latency across both workers
    and accumulates retriever / rerank from every evidence_retriever
    handoff (a re-query within a turn would hit this case)."""

    intake = Handoff(
        worker="intake_extractor",
        tool_use_id="tu-1",
        arguments={},
        output={"facts": []},
        error=None,
        latency_ms=200,
    )
    retrieve = Handoff(
        worker="evidence_retriever",
        tool_use_id="tu-2",
        arguments={},
        output={
            "rerank_backend": "cohere",
            "retriever_ms": 30,
            "rerank_ms": 60,
        },
        error=None,
        latency_ms=100,
    )
    stages = _stage_latencies_from_handoffs(_sup((intake, retrieve)), total_ms=500)
    assert stages == {
        "total": 500,
        "supervisor_dispatch": 300,
        "retriever": 30,
        "rerank": 60,
    }


def test_evidence_retriever_error_handoff_skipped_for_per_stage_keys() -> None:
    """A worker-error handoff has ``output is None``. ``supervisor_dispatch``
    must still include its dispatch latency (we paid for the round-trip)
    but the per-stage retriever / rerank keys cannot be inferred from
    a missing payload."""

    handoff = Handoff(
        worker="evidence_retriever",
        tool_use_id="tu-4",
        arguments={},
        output=None,
        error="boom",
        latency_ms=70,
    )
    stages = _stage_latencies_from_handoffs(_sup((handoff,)), total_ms=200)
    assert stages == {"total": 200, "supervisor_dispatch": 70}
