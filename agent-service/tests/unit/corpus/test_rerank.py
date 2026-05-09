"""Unit smoke for the LLM-judge rerank.

The Anthropic client is mocked. Tests cover:

* normal rerank path: judge returns scores, chunks are re-sorted;
* malformed JSON falls back to BM25 order;
* API error falls back to BM25 order;
* empty candidates returns empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from clinical_copilot.corpus.rerank import _parse_scores, rerank_with_llm
from clinical_copilot.corpus.retriever import RetrievedChunk


def _chunk(chunk_id: str, score: float = 0.5) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_doc_id="src",
        title="Title",
        source="USPSTF",
        source_url="https://example.test",
        text="lorem ipsum",
        score=score,
    )


@dataclass
class _FakeText:
    text: str
    type: str = "text"


@dataclass
class _FakeMessage:
    content: list[_FakeText]


def _client_with_json(json_blob: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = _FakeMessage(content=[_FakeText(text=json_blob)])
    return client


def test_rerank_resorts_by_judge_scores() -> None:
    chunks = [_chunk("c1", score=0.9), _chunk("c2", score=0.5), _chunk("c3", score=0.3)]
    client = _client_with_json(
        '{"scores": [{"chunk_id": "c1", "score": 0.2},'
        ' {"chunk_id": "c2", "score": 0.95},'
        ' {"chunk_id": "c3", "score": 0.6}]}'
    )
    out = rerank_with_llm(client=client, query="x", candidates=chunks, top_k=3)
    assert [c.chunk_id for c in out] == ["c2", "c3", "c1"]


def test_rerank_top_k_truncates() -> None:
    chunks = [_chunk(f"c{i}", score=0.5) for i in range(5)]
    client = _client_with_json(
        '{"scores": ['
        + ",".join(f'{{"chunk_id": "c{i}", "score": {1.0 - i * 0.1}}}' for i in range(5))
        + "]}"
    )
    out = rerank_with_llm(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c0", "c1"]


def test_rerank_falls_back_on_malformed_json() -> None:
    chunks = [_chunk("c1"), _chunk("c2")]
    client = _client_with_json("definitely not json")
    out = rerank_with_llm(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_rerank_falls_back_on_api_error() -> None:
    chunks = [_chunk("c1"), _chunk("c2")]
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    out = rerank_with_llm(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_rerank_empty_candidates() -> None:
    client = MagicMock()
    out = rerank_with_llm(client=client, query="x", candidates=[], top_k=5)
    assert out == []
    client.messages.create.assert_not_called()


def test_parse_scores_clamps_out_of_range() -> None:
    result = _parse_scores(
        '{"scores": [{"chunk_id": "c1", "score": 1.5}, {"chunk_id": "c2", "score": -0.3}]}'
    )
    assert result == {"c1": 1.0, "c2": 0.0}


def test_parse_scores_returns_none_on_missing_scores_key() -> None:
    assert _parse_scores('{"foo": "bar"}') is None


def test_rerank_emits_success_log() -> None:
    """Symmetric to the Cohere success-log test — operators
    tail-grep this event to confirm which backend ran on a request."""
    from structlog.testing import capture_logs

    chunks = [_chunk("c1", score=0.9), _chunk("c2", score=0.5)]
    client = _client_with_json(
        '{"scores": [{"chunk_id": "c1", "score": 0.95}, {"chunk_id": "c2", "score": 0.3}]}'
    )
    with capture_logs() as logs:
        rerank_with_llm(client=client, query="x", candidates=chunks, top_k=2)
    success_logs = [log for log in logs if log.get("event") == "corpus.rerank.llm_judge_ok"]
    assert len(success_logs) == 1
    entry = success_logs[0]
    assert entry["n_in"] == 2
    assert entry["n_out"] == 2
    assert entry["top_chunk_id"] == "c1"
    assert entry["top_score"] == 0.95
    assert isinstance(entry["latency_ms"], int)
    assert entry["latency_ms"] >= 0


def test_rerank_does_not_log_success_on_api_error() -> None:
    from structlog.testing import capture_logs
    from unittest.mock import MagicMock

    chunks = [_chunk("c1"), _chunk("c2")]
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    with capture_logs() as logs:
        rerank_with_llm(client=client, query="x", candidates=chunks, top_k=2)
    success_logs = [log for log in logs if log.get("event") == "corpus.rerank.llm_judge_ok"]
    assert success_logs == []
    error_logs = [log for log in logs if log.get("event") == "corpus.rerank.api_error"]
    assert len(error_logs) == 1
