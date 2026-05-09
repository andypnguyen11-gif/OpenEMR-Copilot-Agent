"""Backend-selection coverage for ``run_evidence_retriever``.

The W2-RR change made the worker select between three rerank backends
(Cohere primary / LLM-judge fallback / pure BM25). This test pins the
selection logic so a future wiring regression can't silently demote
Cohere to the LLM-judge path on a deploy that did set
``COHERE_API_KEY``.

Both rerank functions are monkey-patched so this stays a pure unit test
— no Anthropic or Cohere SDK touched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from clinical_copilot.corpus import rerank as rerank_module
from clinical_copilot.corpus.retriever import RetrievedChunk
from clinical_copilot.orchestrator.workers import evidence_retriever as worker_module
from clinical_copilot.orchestrator.workers.evidence_retriever import (
    EvidenceRetrieverOutput,
    run_evidence_retriever,
)


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_doc_id="src",
        title="Title",
        source="USPSTF",
        source_url="https://example.test",
        text="lorem ipsum",
        score=0.5,
    )


class _FakeRetriever:
    """Minimal stand-in for :class:`CorpusRetriever` — just returns a
    fixed candidate list and exposes ``hybrid_enabled`` for the
    output dataclass."""

    hybrid_enabled = False

    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self._chunks = chunks

    def retrieve(self, *, query: str, k: int) -> list[RetrievedChunk]:
        return self._chunks[:k]


@pytest.fixture
def retriever() -> _FakeRetriever:
    return _FakeRetriever([_chunk("c1"), _chunk("c2"), _chunk("c3")])


def _patch_rerank_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Replace both rerank functions with mocks so we can assert which
    one ran without invoking either real backend."""

    from clinical_copilot.observability.traces import UsageTotals

    cohere_mock = MagicMock(return_value=[_chunk("c1")])
    llm_mock = MagicMock(return_value=([_chunk("c2")], UsageTotals(input_tokens=12, output_tokens=4)))
    monkeypatch.setattr(rerank_module, "rerank_with_cohere", cohere_mock)
    monkeypatch.setattr(rerank_module, "rerank_with_llm", llm_mock)
    monkeypatch.setattr(worker_module, "rerank_with_cohere", cohere_mock)
    monkeypatch.setattr(worker_module, "rerank_with_llm", llm_mock)
    return {"cohere": cohere_mock, "llm": llm_mock}


def test_cohere_wins_when_both_clients_present(
    retriever: _FakeRetriever,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _patch_rerank_calls(monkeypatch)
    output = run_evidence_retriever(
        retriever=retriever,
        query="x",
        k=2,
        rerank_client=MagicMock(),
        cohere_client=MagicMock(),
    )
    assert output.rerank_backend == "cohere"
    assert output.reranked is True
    mocks["cohere"].assert_called_once()
    mocks["llm"].assert_not_called()
    # to_tool_result must surface the backend so the supervisor's audit
    # trail records which reranker actually ran.
    assert output.to_tool_result()["rerank_backend"] == "cohere"


def test_llm_judge_runs_when_only_anthropic_passed(
    retriever: _FakeRetriever,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _patch_rerank_calls(monkeypatch)
    output = run_evidence_retriever(
        retriever=retriever,
        query="x",
        k=2,
        rerank_client=MagicMock(),
        cohere_client=None,
    )
    assert output.rerank_backend == "llm_judge"
    assert output.reranked is True
    mocks["llm"].assert_called_once()
    mocks["cohere"].assert_not_called()


def test_pure_bm25_when_no_rerank_clients(
    retriever: _FakeRetriever,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _patch_rerank_calls(monkeypatch)
    output = run_evidence_retriever(
        retriever=retriever,
        query="x",
        k=2,
        rerank_client=None,
        cohere_client=None,
    )
    assert output.rerank_backend == "bm25_only"
    assert output.reranked is False
    mocks["cohere"].assert_not_called()
    mocks["llm"].assert_not_called()


def test_default_rerank_backend_is_bm25_only() -> None:
    # Direct construction (cassette / fixture path) must default to
    # ``bm25_only`` so existing tests that build the dataclass don't
    # have to be updated when no reranker actually ran.
    out: EvidenceRetrieverOutput = EvidenceRetrieverOutput(
        query="x",
        chunks=[],
        hybrid_enabled=False,
        reranked=False,
    )
    assert out.rerank_backend == "bm25_only"
    payload: dict[str, Any] = out.to_tool_result()
    assert payload["rerank_backend"] == "bm25_only"


# ----------------------------------------------------- chunk-dict citation shape


def test_chunk_dict_citation_parses_as_guideline_citation(
    retriever: _FakeRetriever,
) -> None:
    """The ``citation`` block on each chunk dict round-trips through
    ``GuidelineCitation.model_validate`` — the wire-shape carries a
    typed citation discriminated on ``source_type="guideline"``."""

    from clinical_copilot.documents.schemas.citation import GuidelineCitation

    output = run_evidence_retriever(retriever=retriever, query="afib", k=2)
    assert len(output.chunks) == 2
    for chunk in output.chunks:
        citation_dict = chunk["citation"]
        rebuilt = GuidelineCitation.model_validate(citation_dict)
        assert rebuilt.source_type == "guideline"
        assert rebuilt.chunk_id == chunk["chunk_id"]
        assert rebuilt.field_or_chunk_id == chunk["chunk_id"]
        assert rebuilt.source_doc_id == chunk["source_doc_id"]
        assert 0.0 <= rebuilt.confidence <= 1.0


def test_chunk_dict_citation_clamps_out_of_range_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BM25 raw scores are unbounded; the citation's ``confidence``
    field is ``[0, 1]``-validated. An out-of-range score must be
    clamped, not crash the response build."""

    from clinical_copilot.documents.schemas.citation import GuidelineCitation

    over = RetrievedChunk(
        chunk_id="c-over", source_doc_id="s", title="t", source="src",
        source_url="https://example.test", text="...", score=42.0,
    )
    under = RetrievedChunk(
        chunk_id="c-under", source_doc_id="s", title="t", source="src",
        source_url="https://example.test", text="...", score=-1.0,
    )
    fake = _FakeRetriever([over, under])
    output = run_evidence_retriever(retriever=fake, query="x", k=2)
    cits = [GuidelineCitation.model_validate(c["citation"]) for c in output.chunks]
    assert cits[0].confidence == 1.0
    assert cits[1].confidence == 0.0
