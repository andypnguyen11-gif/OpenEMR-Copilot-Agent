"""Rerank stage for the corpus retriever.

Phase 3 of the Week 2 plan: BM25 stays as the first-stage lexical
retriever; this module is the rerank stage that the supervisor's
``evidence_retriever`` worker calls between
``CorpusRetriever.retrieve`` and the synthesis step.

Two backends share the same best-effort contract:

* :func:`rerank_with_cohere` — Cohere ``rerank-v3.5`` cross-encoder
  (Sunday primary; promoted from the post-Sunday queue 2026-05-08).
  ~80 ms p50, well-calibrated relevance scores, ~$2/1k searches.
* :func:`rerank_with_llm` — Claude Haiku scoring each candidate
  (env-var-gated fallback when ``COHERE_API_KEY`` is absent or the
  Cohere call errors). ~600 ms p50.

Contract (both backends)
========================

* Input: query + at most :data:`MAX_CANDIDATES` :class:`RetrievedChunk`
  objects from the BM25 stage.
* Each candidate is scored on a 0..1 relevance scale.
* Output: chunks re-sorted by rerank score, BM25 score broken out as
  a tie-break key, top-k returned.
* Failure modes (API error, malformed response, empty results) are
  logged and fall back to the input order — never raise to the caller.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from anthropic import Anthropic

from clinical_copilot.corpus.retriever import RetrievedChunk
from clinical_copilot.observability.traces import UsageTotals

logger = structlog.get_logger(__name__)

DEFAULT_RERANK_MODEL = "claude-haiku-4-5"
"""Cheap, fast model — judges don't need Sonnet-level reasoning."""

DEFAULT_COHERE_RERANK_MODEL = "rerank-v3.5"
"""Cohere's general-purpose multilingual reranker (current as of
2026-05). Stays accurate on clinical-guideline text without a domain
fine-tune; the model name is pinned so a Cohere SDK upgrade can't
silently change scoring behaviour underneath the eval gate."""

MAX_COHERE_DOC_CHARS = 2000
"""Per-document cap on the text payload sent to Cohere. The chunker
emits 200–400 token chunks (~1–1.5k chars), so this is a generous
ceiling — catches a pathological long chunk without truncating any
real corpus document."""

MAX_CANDIDATES = 20
"""Cap on candidates the judge sees in one turn. Larger inputs blow
the prompt budget and degrade scoring quality. The retriever is
expected to send at most this many."""

MAX_OUTPUT_TOKENS = 600
"""Per-call cap. The judge emits one line per chunk plus minor
overhead; 600 tokens is generous for 20 chunks."""

RERANK_SYSTEM_PROMPT = """\
You are a clinical-evidence relevance judge. You will be given a
clinician's query and a list of candidate guideline excerpts. For
each excerpt, score how relevant it is to answering the query, on a
scale from 0.0 (irrelevant) to 1.0 (directly answers the query).

Reply with ONLY a single JSON object on one line, no prose, no
markdown fences:

{"scores": [{"chunk_id": "...", "score": 0.0}]}

Score every chunk you were given, in the same order, using their
chunk_id verbatim. Do not invent chunk_ids. Higher score means more
relevant.
"""


@dataclass(frozen=True, slots=True)
class RerankedChunk:
    """A :class:`RetrievedChunk` plus the rerank stage's score.

    The original ``score`` (BM25) is preserved for tie-breaks and for
    the supervisor's logging — a chunk with a high BM25 hit and a low
    rerank score is informative ("lexically matches but semantically
    off-topic")."""

    chunk: RetrievedChunk
    rerank_score: float


def rerank_with_llm(
    *,
    client: Anthropic,
    query: str,
    candidates: list[RetrievedChunk],
    model: str = DEFAULT_RERANK_MODEL,
    top_k: int = 5,
) -> tuple[list[RetrievedChunk], UsageTotals]:
    """Score, sort, and return the top-k chunks plus per-call usage.

    Best-effort: any failure (API error, malformed JSON, missing
    chunk_ids) falls back to the input order capped at ``top_k`` so
    the caller never has to handle rerank-stage exceptions. Returns
    ``(chunks, UsageTotals)``; usage is zero on the early-return paths
    where no Anthropic call ran (or the call raised before ``response``
    was bound) so the caller can fold the result into a running total
    unconditionally.
    """

    if not candidates:
        return [], UsageTotals()
    if top_k <= 0:
        return [], UsageTotals()
    candidates_capped = candidates[:MAX_CANDIDATES]
    user_payload = _build_user_message(query=query, chunks=candidates_capped)

    t0 = time.perf_counter()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=RERANK_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
        )
    except Exception as exc:
        logger.warning(
            "corpus.rerank.api_error",
            error=f"{type(exc).__name__}: {exc}",
            n_candidates=len(candidates_capped),
        )
        return candidates_capped[:top_k], UsageTotals()

    usage = _usage_from_message(response)

    text = "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", "") == "text"
    )
    scores_by_id = _parse_scores(text)
    if scores_by_id is None:
        logger.warning("corpus.rerank.parse_failed", text_len=len(text))
        return candidates_capped[:top_k], usage

    reranked: list[RerankedChunk] = []
    for chunk in candidates_capped:
        score = scores_by_id.get(chunk.chunk_id)
        if score is None:
            # Judge didn't score this chunk — fall back to BM25 score
            # rather than dropping it.
            reranked.append(RerankedChunk(chunk=chunk, rerank_score=chunk.score))
        else:
            reranked.append(RerankedChunk(chunk=chunk, rerank_score=score))

    reranked.sort(key=lambda r: (r.rerank_score, r.chunk.score), reverse=True)
    result = [r.chunk for r in reranked[:top_k]]
    logger.info(
        "corpus.rerank.llm_judge_ok",
        n_in=len(candidates_capped),
        n_out=len(result),
        top_chunk_id=result[0].chunk_id if result else None,
        top_score=reranked[0].rerank_score if reranked else None,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )
    return result, usage


def _usage_from_message(message: Any) -> UsageTotals:
    """Pull ``response.usage`` off an Anthropic ``Message`` shape.

    Defensive: zero on missing fields so this never crashes the rerank
    stage's best-effort contract for an absent usage rollup.
    """

    usage = getattr(message, "usage", None)
    return UsageTotals(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def _build_user_message(*, query: str, chunks: list[RetrievedChunk]) -> str:
    lines = [f"Query: {query}", "", "Candidates:"]
    for c in chunks:
        # Truncate text to keep the prompt budget predictable. 400
        # characters is plenty for a chunk excerpt's gist.
        text_excerpt = c.text[:400].replace("\n", " ")
        lines.append(
            f"- chunk_id={c.chunk_id} (source={c.source}, title={c.title!r}): {text_excerpt}"
        )
    return "\n".join(lines)


class CohereRerankClient(Protocol):
    """Structural interface over the bit of the Cohere SDK we use.

    Defining a Protocol decouples this module from a hard
    ``import cohere`` at type-check time and lets the unit test mock
    the rerank call without pulling the SDK into the test
    environment. The real client is :class:`cohere.ClientV2`, whose
    ``rerank`` method matches this signature.
    """

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        top_n: int,
    ) -> Any: ...


def rerank_with_cohere(
    *,
    client: CohereRerankClient,
    query: str,
    candidates: list[RetrievedChunk],
    model: str = DEFAULT_COHERE_RERANK_MODEL,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """Score, sort, and return the top-k chunks via Cohere Rerank.

    Best-effort: any failure (API error, empty response, indices that
    don't map back to the input) falls back to the input order capped
    at ``top_k`` so the caller never has to handle rerank-stage
    exceptions. Mirrors the contract of :func:`rerank_with_llm`.
    """

    if not candidates:
        return []
    if top_k <= 0:
        return []
    candidates_capped = candidates[:MAX_CANDIDATES]
    documents = [_chunk_payload(c) for c in candidates_capped]
    requested_top_n = min(top_k, len(candidates_capped))

    t0 = time.perf_counter()
    try:
        response = client.rerank(
            query=query,
            documents=documents,
            model=model,
            top_n=requested_top_n,
        )
    except Exception as exc:
        logger.warning(
            "corpus.rerank.cohere_api_error",
            error=f"{type(exc).__name__}: {exc}",
            n_candidates=len(candidates_capped),
        )
        return candidates_capped[:top_k]

    results = getattr(response, "results", None)
    if not results:
        logger.warning(
            "corpus.rerank.cohere_empty_results",
            n_candidates=len(candidates_capped),
        )
        return candidates_capped[:top_k]

    ordered: list[RerankedChunk] = []
    seen_indices: set[int] = set()
    n = len(candidates_capped)
    for result in results:
        index = getattr(result, "index", None)
        score = getattr(result, "relevance_score", None)
        if not isinstance(index, int) or index < 0 or index >= n:
            continue
        if index in seen_indices:
            continue
        if not isinstance(score, (int, float)):
            continue
        seen_indices.add(index)
        ordered.append(
            RerankedChunk(
                chunk=candidates_capped[index],
                rerank_score=max(0.0, min(1.0, float(score))),
            ),
        )

    if not ordered:
        # Every result row was malformed — degrade to BM25 order rather
        # than silently dropping every chunk.
        logger.warning(
            "corpus.rerank.cohere_unusable_results",
            n_candidates=len(candidates_capped),
            n_results=len(results),
        )
        return candidates_capped[:top_k]

    ordered.sort(key=lambda r: (r.rerank_score, r.chunk.score), reverse=True)
    result = [r.chunk for r in ordered[:top_k]]
    logger.info(
        "corpus.rerank.cohere_ok",
        n_in=len(candidates_capped),
        n_out=len(result),
        top_chunk_id=result[0].chunk_id if result else None,
        top_score=ordered[0].rerank_score if ordered else None,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )
    return result


def _chunk_payload(chunk: RetrievedChunk) -> str:
    """Format a chunk for the Cohere rerank request body.

    Includes title + source so the cross-encoder has the same
    surface signal the LLM judge sees in its prompt; truncates to
    :data:`MAX_COHERE_DOC_CHARS` so a pathological long chunk can't
    blow the per-request payload budget.
    """

    body = chunk.text[:MAX_COHERE_DOC_CHARS]
    return f"{chunk.title} ({chunk.source})\n{body}"


def _parse_scores(text: str) -> dict[str, float] | None:
    """Parse the judge's one-line JSON; return None on any malformed
    structure so the caller can fall back."""

    text = text.strip()
    # Tolerate leading/trailing prose by extracting the first {...} blob.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    rows = parsed.get("scores")
    if not isinstance(rows, list):
        return None
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        chunk_id = row.get("chunk_id")
        score = row.get("score")
        if not isinstance(chunk_id, str) or not isinstance(score, (int, float)):
            continue
        out[chunk_id] = max(0.0, min(1.0, float(score)))
    return out or None
