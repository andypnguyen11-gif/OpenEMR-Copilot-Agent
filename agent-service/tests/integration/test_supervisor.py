"""End-to-end smoke for the supervisor + 2 workers.

The Anthropic client is mocked — we drive ``messages.create`` to return
a scripted sequence of (tool_use, tool_use, text) responses so the
supervisor exercises both workers and the synthesis exit. No live
LLM, no live retriever, no live extractor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from clinical_copilot.orchestrator import supervisor
from clinical_copilot.orchestrator.supervisor import SupervisorResponse

# --------------------------------------------------------------- helpers


@dataclass
class _FakeToolUseBlock:
    """Stand-in for ``anthropic.types.ToolUseBlock``.

    The supervisor uses ``isinstance(block, ToolUseBlock)`` to detect
    tool dispatches, so we monkey-patch that import in the test.
    """

    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeMessage:
    content: list[Any]


def _patch_isinstance(monkeypatch) -> None:
    """Make the supervisor's ``isinstance(block, ToolUseBlock)`` accept
    our fake block class."""

    monkeypatch.setattr(supervisor, "ToolUseBlock", _FakeToolUseBlock)


def _build_client(responses: list[_FakeMessage]) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = responses
    return client


# --------------------------------------------------------------- tests


def test_supervisor_mixed_query_dispatches_both_workers(monkeypatch) -> None:
    _patch_isinstance(monkeypatch)

    intake_calls: list[dict[str, Any]] = []
    evidence_calls: list[dict[str, Any]] = []

    def fake_intake(**kwargs: Any) -> dict[str, Any]:
        intake_calls.append(kwargs)
        return {
            "document_id": "lab-001",
            "document_type": "lab_pdf",
            "facts": {"document_id": "lab-001", "observations": []},
            "citations": [],
        }

    def fake_evidence(**kwargs: Any) -> dict[str, Any]:
        evidence_calls.append(kwargs)
        return {
            "query": kwargs["query"],
            "chunks": [{"chunk_id": "c1", "source_doc_id": "uspstf-2023"}],
            "hybrid_enabled": False,
        }

    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_intake_extractor",
                        input={
                            "document_path": "/tmp/lab.pdf",
                            "document_type": "lab_pdf",
                        },
                    ),
                    _FakeToolUseBlock(
                        id="t2",
                        name="dispatch_evidence_retriever",
                        input={"query": "lipid management", "k": 3},
                    ),
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="LDL is 158 (high) — see USPSTF guidance.")]),
        ]
    )

    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="What does the recent lab show and what guidelines apply?",
        intake_extractor=fake_intake,
        evidence_retriever=fake_evidence,
    )

    assert isinstance(response, SupervisorResponse)
    assert "LDL is 158" in response.synthesized_text
    assert response.iterations == 2
    assert len(response.handoffs) == 2
    workers = sorted(h.worker for h in response.handoffs)
    assert workers == ["evidence_retriever", "intake_extractor"]
    assert intake_calls == [{"document_path": "/tmp/lab.pdf", "document_type": "lab_pdf"}]
    assert evidence_calls == [{"query": "lipid management", "k": 3}]


def test_supervisor_document_only_query(monkeypatch) -> None:
    _patch_isinstance(monkeypatch)
    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_intake_extractor",
                        input={
                            "document_path": "/tmp/intake.pdf",
                            "document_type": "intake_form",
                        },
                    )
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="Patient denies allergies.")]),
        ]
    )
    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="What does the intake form say?",
        intake_extractor=lambda **k: {"facts": {}, "citations": []},
        evidence_retriever=lambda **k: {"chunks": []},
    )
    assert response.iterations == 2
    assert [h.worker for h in response.handoffs] == ["intake_extractor"]
    assert response.abstention_reason is None


def test_supervisor_iteration_cap_abstains_with_tool_failure(monkeypatch) -> None:
    _patch_isinstance(monkeypatch)
    # Every turn returns a tool_use block — the model never finishes.
    looping = [
        _FakeMessage(
            content=[
                _FakeToolUseBlock(
                    id=f"t{i}",
                    name="dispatch_evidence_retriever",
                    input={"query": "x"},
                )
            ]
        )
        for i in range(10)
    ]
    client = _build_client(looping)
    # AAISP-2026-0001 budget gate now fires before the iteration cap; this
    # test pins the iteration-cap path so we raise the tool-call ceiling
    # out of the way. The gate itself is covered by
    # test_supervisor_aaisp_budget_gate.py.
    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="what",
        intake_extractor=lambda **k: {},
        evidence_retriever=lambda **k: {"chunks": []},
        max_iterations=3,
        max_tool_calls_per_turn=99,
    )
    assert response.abstention_reason == "TOOL_FAILURE"
    assert response.iterations == 3


def test_supervisor_worker_error_records_handoff_with_error(monkeypatch) -> None:
    _patch_isinstance(monkeypatch)
    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_intake_extractor",
                        input={"document_path": "/missing", "document_type": "lab_pdf"},
                    )
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="cannot synthesize without facts")]),
        ]
    )

    def failing_intake(**kwargs: Any) -> dict[str, Any]:
        raise FileNotFoundError("nope")

    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="extract this",
        intake_extractor=failing_intake,
        evidence_retriever=lambda **k: {"chunks": []},
    )
    assert len(response.handoffs) == 1
    assert response.handoffs[0].error is not None
    assert "nope" in response.handoffs[0].error
    assert response.handoffs[0].output is None


def test_supervisor_rerank_backend_surfaces_from_evidence_handoff(monkeypatch) -> None:
    """The supervisor stamps the evidence-retriever's ``rerank_backend``
    onto :class:`SupervisorResponse` so the wire shape can echo which
    backend served the synthesis."""

    _patch_isinstance(monkeypatch)
    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_evidence_retriever",
                        input={"query": "afib", "k": 3},
                    ),
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="see USPSTF guidance")]),
        ]
    )

    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="afib management?",
        intake_extractor=lambda **k: {},
        evidence_retriever=lambda **k: {
            "query": k["query"],
            "chunks": [],
            "hybrid_enabled": False,
            "reranked": True,
            "rerank_backend": "cohere",
        },
    )
    assert response.rerank_backend == "cohere"


def test_supervisor_rerank_backend_none_when_only_intake_dispatched(monkeypatch) -> None:
    """A turn that never calls the evidence retriever leaves
    ``rerank_backend`` as ``None`` — the UI badge stays off."""

    _patch_isinstance(monkeypatch)
    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_intake_extractor",
                        input={
                            "document_path": "/tmp/lab.pdf",
                            "document_type": "lab_pdf",
                        },
                    ),
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="LDL is high")]),
        ]
    )

    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="what does the lab say?",
        intake_extractor=lambda **k: {"facts": {}, "citations": []},
        evidence_retriever=lambda **k: {"chunks": []},
    )
    assert response.rerank_backend is None


def test_supervisor_parallel_dispatch_collapses_wall_clock(monkeypatch) -> None:
    """Two tool_use blocks in one supervisor turn dispatch in parallel.

    Each fake worker sleeps 250 ms; serial dispatch would clock ~500 ms,
    parallel ~250 ms. We assert the elapsed is < 400 ms — wider than
    250 to absorb thread-pool warm-up + the no-op Anthropic mock,
    tighter than 500 to fail loudly on a regression to serial.

    The two-block ``_FakeMessage`` returns both extractor + retriever
    in a single response, which is exactly the pattern (mixed
    "extract these labs AND give me the matching guideline") that the
    serial path was costing the most p95 on.
    """

    import time

    _patch_isinstance(monkeypatch)

    def slow_intake(**kwargs: Any) -> dict[str, Any]:
        time.sleep(0.25)
        return {"facts": {}, "citations": []}

    def slow_evidence(**kwargs: Any) -> dict[str, Any]:
        time.sleep(0.25)
        return {"chunks": []}

    client = _build_client(
        [
            _FakeMessage(
                content=[
                    _FakeToolUseBlock(
                        id="t1",
                        name="dispatch_intake_extractor",
                        input={"document_path": "x.pdf", "document_type": "lab_pdf"},
                    ),
                    _FakeToolUseBlock(
                        id="t2",
                        name="dispatch_evidence_retriever",
                        input={"query": "atrial fibrillation rate control"},
                    ),
                ]
            ),
            _FakeMessage(content=[_FakeTextBlock(text="grounded synthesis")]),
        ]
    )

    started = time.perf_counter()
    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="extract this lab and find the matching guideline",
        intake_extractor=slow_intake,
        evidence_retriever=slow_evidence,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    assert len(response.handoffs) == 2
    workers = sorted(h.worker for h in response.handoffs)
    assert workers == ["evidence_retriever", "intake_extractor"]
    assert elapsed_ms < 400, (
        f"parallel dispatch should collapse to ~250ms; got {elapsed_ms}ms "
        "(serial regression?)"
    )
