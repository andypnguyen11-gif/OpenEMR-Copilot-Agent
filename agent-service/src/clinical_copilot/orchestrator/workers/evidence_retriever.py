"""``evidence_retriever`` worker — wraps the hybrid corpus retriever.

The supervisor invokes this worker by emitting an Anthropic
``tool_use`` block for ``dispatch_evidence_retriever`` with arguments

::

    {"query": "atrial fibrillation rate control", "k": 5}

The worker calls
:meth:`clinical_copilot.corpus.retriever.CorpusRetriever.retrieve`
and returns a JSON-serializable dict the supervisor can stitch into
its synthesis. Each chunk carries a ``citation`` field shaped like the
document-side :class:`SourceCitation` so the supervisor's "no uncited
claim" check can treat lab-PDF and corpus citations uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from clinical_copilot.corpus.rerank import rerank_with_llm
from clinical_copilot.corpus.retriever import CorpusRetriever, RetrievedChunk


class WorkerError(RuntimeError):
    """Raised on invalid input or retriever failure."""


@dataclass(frozen=True, slots=True)
class EvidenceRetrieverOutput:
    """Structured worker output."""

    query: str
    chunks: list[dict[str, Any]]
    hybrid_enabled: bool
    reranked: bool

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "chunks": self.chunks,
            "hybrid_enabled": self.hybrid_enabled,
            "reranked": self.reranked,
        }


def run_evidence_retriever(
    *,
    retriever: CorpusRetriever,
    query: str,
    k: int = 5,
    rerank_client: Anthropic | None = None,
    rerank_model: str | None = None,
    candidate_multiplier: int = 3,
) -> EvidenceRetrieverOutput:
    """Validate inputs, retrieve candidates, optionally rerank, return.

    When ``rerank_client`` is provided the worker fetches
    ``k * candidate_multiplier`` candidates from the BM25 stage (capped
    at the rerank module's ``MAX_CANDIDATES``) and asks an LLM judge to
    re-score them. The top-k after rerank ships back to the supervisor.

    When ``rerank_client`` is ``None`` the worker behaves as a pure
    pass-through over BM25 — that's the offline / no-network test path
    and the documented degradation when the rerank stage is disabled.
    """

    if not query or not query.strip():
        raise WorkerError("query must be a non-empty string")
    if k <= 0 or k > 50:
        raise WorkerError(f"k must be in (0, 50], got {k}")

    if rerank_client is None:
        chunks = retriever.retrieve(query=query, k=k)
        reranked = False
    else:
        candidate_k = max(k, k * max(1, candidate_multiplier))
        candidates = retriever.retrieve(query=query, k=candidate_k)
        kwargs: dict[str, Any] = {
            "client": rerank_client,
            "query": query,
            "candidates": candidates,
            "top_k": k,
        }
        if rerank_model:
            kwargs["model"] = rerank_model
        chunks = rerank_with_llm(**kwargs)
        reranked = True

    return EvidenceRetrieverOutput(
        query=query,
        chunks=[_chunk_to_dict(c) for c in chunks],
        hybrid_enabled=retriever.hybrid_enabled,
        reranked=reranked,
    )


def _chunk_to_dict(c: RetrievedChunk) -> dict[str, Any]:
    """Project a :class:`RetrievedChunk` to the supervisor's tool-result
    shape with a citation block matching SourceCitation conventions."""

    return {
        "chunk_id": c.chunk_id,
        "source_doc_id": c.source_doc_id,
        "title": c.title,
        "source": c.source,
        "source_url": c.source_url,
        "text": c.text,
        "score": c.score,
        "citation": {
            "source": c.source,
            "source_url": c.source_url,
            "source_doc_id": c.source_doc_id,
            "chunk_id": c.chunk_id,
        },
    }
