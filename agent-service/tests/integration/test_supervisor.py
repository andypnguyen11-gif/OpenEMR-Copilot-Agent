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
    response = supervisor.run(
        client=client,
        model="claude-sonnet-4",
        query="what",
        intake_extractor=lambda **k: {},
        evidence_retriever=lambda **k: {"chunks": []},
        max_iterations=3,
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
