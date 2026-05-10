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

import time
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from clinical_copilot.corpus.rerank import (
    CohereRerankClient,
    rerank_with_cohere,
    rerank_with_llm,
)
from clinical_copilot.corpus.retriever import CorpusRetriever, RetrievedChunk
from clinical_copilot.documents.schemas.citation import GuidelineCitation
from clinical_copilot.observability.traces import UsageTotals

RerankBackend = str
"""``"cohere"`` | ``"llm_judge"`` | ``"bm25_only"`` — recorded on
:class:`EvidenceRetrieverOutput` so the supervisor's audit row names
the actual reranker used. Plain ``str`` rather than an Enum keeps
JSON serialization and tool-result payloads trivial. Underscore form
is the canonical wire spelling reflected on
:class:`AgentResponse.rerank_backend`; older deploys / cassettes that
emitted ``"llm-judge"`` / ``"none"`` are mapped at the wire boundary
in :mod:`main`."""


class WorkerError(RuntimeError):
    """Raised on invalid input or retriever failure."""


@dataclass(frozen=True, slots=True)
class EvidenceRetrieverOutput:
    """Structured worker output."""

    query: str
    chunks: list[dict[str, Any]]
    hybrid_enabled: bool
    reranked: bool
    # ``"bm25_only"`` default keeps any direct constructor / cassette
    # test working without modification; the worker overrides it
    # per-call based on which backend actually ran. Underscore spelling
    # matches the wire shape on :class:`AgentResponse.rerank_backend`.
    rerank_backend: RerankBackend = "bm25_only"
    # Token totals from the rerank stage (LLM-judge backend only —
    # Cohere rerank uses a separate API and returns ``UsageTotals(0, 0)``
    # here). Surfaced on the tool-result payload so the supervisor can
    # fold it into the run-wide ``UsageTotals`` for the trace row.
    usage_totals: UsageTotals = UsageTotals()
    # Per-stage timings. ``retriever_ms`` covers the BM25 + dense union
    # + RRF fusion pass; ``rerank_ms`` covers the active reranker's
    # call. Both are surfaced on the tool-result so the supervisor can
    # fold them into ``AgentResponse.stage_latencies_ms`` without
    # wrapping its own perf_counter around the worker call (which would
    # only see the outer round-trip and miss the per-stage split).
    retriever_ms: int = 0
    rerank_ms: int = 0

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "chunks": self.chunks,
            "hybrid_enabled": self.hybrid_enabled,
            "reranked": self.reranked,
            "rerank_backend": self.rerank_backend,
            "usage_totals": {
                "input_tokens": self.usage_totals.input_tokens,
                "output_tokens": self.usage_totals.output_tokens,
            },
            "retriever_ms": self.retriever_ms,
            "rerank_ms": self.rerank_ms,
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

    rerank_usage = UsageTotals()
    rerank_ms = 0
    if cohere_client is not None:
        candidate_k = max(k, k * max(1, candidate_multiplier))
        retriever_started = time.perf_counter()
        candidates = retriever.retrieve(query=query, k=candidate_k)
        retriever_ms = int((time.perf_counter() - retriever_started) * 1000)
        cohere_kwargs: dict[str, Any] = {
            "client": cohere_client,
            "query": query,
            "candidates": candidates,
            "top_k": k,
        }
        if cohere_model:
            cohere_kwargs["model"] = cohere_model
        rerank_started = time.perf_counter()
        chunks = rerank_with_cohere(**cohere_kwargs)
        rerank_ms = int((time.perf_counter() - rerank_started) * 1000)
        reranked = True
        backend: RerankBackend = "cohere"
    elif rerank_client is not None:
        candidate_k = max(k, k * max(1, candidate_multiplier))
        retriever_started = time.perf_counter()
        candidates = retriever.retrieve(query=query, k=candidate_k)
        retriever_ms = int((time.perf_counter() - retriever_started) * 1000)
        llm_kwargs: dict[str, Any] = {
            "client": rerank_client,
            "query": query,
            "candidates": candidates,
            "top_k": k,
        }
        if rerank_model:
            llm_kwargs["model"] = rerank_model
        rerank_started = time.perf_counter()
        chunks, rerank_usage = rerank_with_llm(**llm_kwargs)
        rerank_ms = int((time.perf_counter() - rerank_started) * 1000)
        reranked = True
        backend = "llm_judge"
    else:
        retriever_started = time.perf_counter()
        chunks = retriever.retrieve(query=query, k=k)
        retriever_ms = int((time.perf_counter() - retriever_started) * 1000)
        reranked = False
        backend = "bm25_only"

    return EvidenceRetrieverOutput(
        query=query,
        chunks=[_chunk_to_dict(c) for c in chunks],
        hybrid_enabled=retriever.hybrid_enabled,
        reranked=reranked,
        rerank_backend=backend,
        usage_totals=rerank_usage,
        retriever_ms=retriever_ms,
        rerank_ms=rerank_ms,
    )


def _chunk_to_dict(c: RetrievedChunk) -> dict[str, Any]:
    """Project a :class:`RetrievedChunk` to the supervisor's tool-result shape.

    The ``citation`` block is the ``GuidelineCitation`` discriminated-union
    member, serialized via ``model_dump`` so the wire shape parses back as
    ``GuidelineCitation`` on the consumer side. ``score`` is clamped to
    ``[0, 1]`` for the ``confidence`` field — BM25 raw scores are unbounded
    (Cohere rerank scores are already in range; LLM-judge scores are bounded
    by the judge prompt). Out-of-range scores would otherwise crash the
    response build for an out-of-band confidence number.
    """

    citation = GuidelineCitation(
        field_or_chunk_id=c.chunk_id,
        source_doc_id=c.source_doc_id,
        chunk_id=c.chunk_id,
        source_url=c.source_url or None,
        confidence=max(0.0, min(1.0, c.score)),
        raw_text=c.text,
    )
    return {
        "chunk_id": c.chunk_id,
        "source_doc_id": c.source_doc_id,
        "title": c.title,
        "source": c.source,
        "source_url": c.source_url,
        "text": c.text,
        "score": c.score,
        "citation": citation.model_dump(mode="json"),
    }
