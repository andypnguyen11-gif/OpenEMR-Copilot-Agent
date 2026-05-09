"""Unit smoke for the Cohere rerank backend.

The Cohere SDK is mocked via the :class:`CohereRerankClient` Protocol —
no ``cohere`` import here. Tests cover:

* normal rerank path: Cohere returns sorted indices, chunks re-sorted;
* API error falls back to BM25 order;
* empty results fallback;
* unusable / malformed result rows fall back to BM25 order;
* top_k truncation;
* duplicate-index dedup;
* per-document payload truncation at MAX_COHERE_DOC_CHARS.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from clinical_copilot.corpus.rerank import (
    MAX_COHERE_DOC_CHARS,
    _chunk_payload,
    rerank_with_cohere,
)
from clinical_copilot.corpus.retriever import RetrievedChunk


def _chunk(chunk_id: str, score: float = 0.5, text: str = "lorem ipsum") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_doc_id="src",
        title="Title",
        source="USPSTF",
        source_url="https://example.test",
        text=text,
        score=score,
    )


@dataclass
class _FakeResult:
    index: int
    relevance_score: float


@dataclass
class _FakeResponse:
    results: list[_FakeResult]


def _client_with_results(results: list[_FakeResult]) -> MagicMock:
    client = MagicMock()
    client.rerank.return_value = _FakeResponse(results=results)
    return client


def test_rerank_resorts_by_cohere_indices() -> None:
    chunks = [_chunk("c1", score=0.9), _chunk("c2", score=0.5), _chunk("c3", score=0.3)]
    # Cohere returns its own ordering: c2 (index 1) wins, then c3 (index 2),
    # then c1 (index 0). The worker must respect the rerank score, not BM25.
    client = _client_with_results(
        [
            _FakeResult(index=1, relevance_score=0.95),
            _FakeResult(index=2, relevance_score=0.6),
            _FakeResult(index=0, relevance_score=0.2),
        ],
    )
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=3)
    assert [c.chunk_id for c in out] == ["c2", "c3", "c1"]


def test_rerank_top_k_truncates() -> None:
    chunks = [_chunk(f"c{i}", score=0.5) for i in range(5)]
    client = _client_with_results(
        [_FakeResult(index=i, relevance_score=1.0 - i * 0.1) for i in range(5)],
    )
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c0", "c1"]


def test_rerank_falls_back_on_api_error() -> None:
    chunks = [_chunk("c1"), _chunk("c2")]
    client = MagicMock()
    client.rerank.side_effect = RuntimeError("boom")
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_rerank_falls_back_on_empty_results() -> None:
    chunks = [_chunk("c1"), _chunk("c2")]
    client = _client_with_results([])
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_rerank_falls_back_on_unusable_results() -> None:
    # Every row is malformed (out-of-range index, non-numeric score) — the
    # backend must degrade to BM25 order rather than returning an empty list.
    chunks = [_chunk("c1"), _chunk("c2")]
    client = _client_with_results(
        [
            _FakeResult(index=99, relevance_score=0.9),  # out of range
            _FakeResult(index=-1, relevance_score=0.8),  # negative index
        ],
    )
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_rerank_dedupes_duplicate_indices() -> None:
    # Cohere shouldn't normally return duplicates, but if it does the
    # worker must keep the first occurrence and drop the rest so a
    # duplicated chunk doesn't crowd out a real one in the top-k.
    chunks = [_chunk("c1", score=0.9), _chunk("c2", score=0.5), _chunk("c3", score=0.3)]
    client = _client_with_results(
        [
            _FakeResult(index=0, relevance_score=0.95),
            _FakeResult(index=0, relevance_score=0.85),  # duplicate, must be ignored
            _FakeResult(index=2, relevance_score=0.6),
        ],
    )
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=3)
    assert [c.chunk_id for c in out] == ["c1", "c3"]


def test_rerank_empty_candidates() -> None:
    client = MagicMock()
    out = rerank_with_cohere(client=client, query="x", candidates=[], top_k=5)
    assert out == []
    client.rerank.assert_not_called()


def test_rerank_top_k_zero_returns_empty() -> None:
    chunks = [_chunk("c1"), _chunk("c2")]
    client = MagicMock()
    out = rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=0)
    assert out == []
    client.rerank.assert_not_called()


def test_chunk_payload_truncates_long_text() -> None:
    # Pathological long chunk — must be capped at MAX_COHERE_DOC_CHARS so a
    # single bad document can't blow the per-request payload budget.
    long_text = "x" * (MAX_COHERE_DOC_CHARS * 2)
    chunk = _chunk("c1", text=long_text)
    payload = _chunk_payload(chunk)
    # Header is "Title (USPSTF)\n" = 15 chars, then truncated body.
    assert payload.endswith("x" * 50)  # spot check that body is present
    assert len(payload) <= MAX_COHERE_DOC_CHARS + 100  # header overhead


def test_chunk_payload_includes_title_and_source() -> None:
    chunk = _chunk("c1", text="body text")
    payload = _chunk_payload(chunk)
    assert payload.startswith("Title (USPSTF)")
    assert "body text" in payload


def test_rerank_emits_success_log() -> None:
    """Operators verify Cohere ran by tail-grep'ing for this event;
    the assertion pins the field shape so a refactor can't silently
    drop ``rerank_backend`` observability."""
    from structlog.testing import capture_logs

    chunks = [_chunk("c1"), _chunk("c2")]
    client = _client_with_results(
        [
            _FakeResult(index=1, relevance_score=0.9),
            _FakeResult(index=0, relevance_score=0.4),
        ],
    )
    with capture_logs() as logs:
        rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    success_logs = [log for log in logs if log.get("event") == "corpus.rerank.cohere_ok"]
    assert len(success_logs) == 1
    entry = success_logs[0]
    assert entry["n_in"] == 2
    assert entry["n_out"] == 2
    assert entry["top_chunk_id"] == "c2"
    assert entry["top_score"] == 0.9
    assert isinstance(entry["latency_ms"], int)
    assert entry["latency_ms"] >= 0


def test_rerank_does_not_log_success_on_api_error() -> None:
    from structlog.testing import capture_logs

    chunks = [_chunk("c1"), _chunk("c2")]
    client = MagicMock()
    client.rerank.side_effect = RuntimeError("boom")
    with capture_logs() as logs:
        rerank_with_cohere(client=client, query="x", candidates=chunks, top_k=2)
    success_logs = [log for log in logs if log.get("event") == "corpus.rerank.cohere_ok"]
    assert success_logs == []
    error_logs = [log for log in logs if log.get("event") == "corpus.rerank.cohere_api_error"]
    assert len(error_logs) == 1
