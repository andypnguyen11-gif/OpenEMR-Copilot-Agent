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

from clinical_copilot.corpus.rerank import (
    CohereRerankClient,
    rerank_with_cohere,
    rerank_with_llm,
)
from clinical_copilot.corpus.retriever import CorpusRetriever, RetrievedChunk

RerankBackend = str
"""``"cohere"`` | ``"llm-judge"`` | ``"none"`` — recorded on
:class:`EvidenceRetrieverOutput` so the supervisor's audit row names
the actual reranker used. Plain ``str`` rather than an Enum keeps
JSON serialization and tool-result payloads trivial."""


class WorkerError(RuntimeError):
    """Raised on invalid input or retriever failure."""


@dataclass(frozen=True, slots=True)
class EvidenceRetrieverOutput:
    """Structured worker output."""

    query: str
    chunks: list[dict[str, Any]]
    hybrid_enabled: bool
    reranked: bool
    # ``"none"`` default keeps any direct constructor / cassette test
    # working without modification; the worker overrides it per-call
    # based on which backend actually ran.
    rerank_backend: RerankBackend = "none"

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "chunks": self.chunks,
            "hybrid_enabled": self.hybrid_enabled,
            "reranked": self.reranked,
            "rerank_backend": self.rerank_backend,
        }


def run_evidence_retriever(
    *,
    retriever: CorpusRetriever,
    query: str,
    k: int = 5,
    rerank_client: Anthropic | None = None,
    rerank_model: str | None = None,
    cohere_client: CohereRerankClient | None = None,
    cohere_model: str | None = None,
    candidate_multiplier: int = 3,
) -> EvidenceRetrieverOutput:
    """Validate inputs, retrieve candidates, optionally rerank, return.

    Backend selection (in order of preference):

    * ``cohere_client`` — Cohere ``rerank-v3.5`` cross-encoder. Primary
      when configured (Sunday-target rerank backend per W2-RR).
    * ``rerank_client`` — Anthropic LLM-judge. Used when only the
      Anthropic client is wired; also the documented fallback path
      when the deploy is missing ``COHERE_API_KEY``.
    * Neither — pure BM25 pass-through; the offline / no-network test
      path and the documented degradation when the rerank stage is
      disabled.

    Both rerank backends are best-effort: any backend-internal failure
    falls back to BM25 order via the rerank module's contract. The
    selection here only chooses *which* backend runs — it does not
    cascade across them on failure (the rerank module already does).
    """

    if not query or not query.strip():
        raise WorkerError("query must be a non-empty string")
    if k <= 0 or k > 50:
        raise WorkerError(f"k must be in (0, 50], got {k}")

    if cohere_client is not None:
        candidate_k = max(k, k * max(1, candidate_multiplier))
        candidates = retriever.retrieve(query=query, k=candidate_k)
        cohere_kwargs: dict[str, Any] = {
            "client": cohere_client,
            "query": query,
            "candidates": candidates,
            "top_k": k,
        }
        if cohere_model:
            cohere_kwargs["model"] = cohere_model
        chunks = rerank_with_cohere(**cohere_kwargs)
        reranked = True
        backend: RerankBackend = "cohere"
    elif rerank_client is not None:
        candidate_k = max(k, k * max(1, candidate_multiplier))
        candidates = retriever.retrieve(query=query, k=candidate_k)
        llm_kwargs: dict[str, Any] = {
            "client": rerank_client,
            "query": query,
            "candidates": candidates,
            "top_k": k,
        }
        if rerank_model:
            llm_kwargs["model"] = rerank_model
        chunks = rerank_with_llm(**llm_kwargs)
        reranked = True
        backend = "llm-judge"
    else:
        chunks = retriever.retrieve(query=query, k=k)
        reranked = False
        backend = "none"

    return EvidenceRetrieverOutput(
        query=query,
        chunks=[_chunk_to_dict(c) for c in chunks],
        hybrid_enabled=retriever.hybrid_enabled,
        reranked=reranked,
        rerank_backend=backend,
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
