"""LLM-judge rerank for the corpus retriever.

Phase 3 of the Week 2 early-submission plan: BM25 stays as the
first-stage lexical retriever; this module is the rerank stage that
the supervisor's ``evidence_retriever`` worker calls between
``CorpusRetriever.retrieve`` and the synthesis step.

A faster cross-encoder (e.g. ``bge-reranker-base``) would beat an
LLM-judge on cost-per-rerank, but adding sentence-transformers blew
up the dep tree for a one-week submission. The LLM judge is the
expedient choice — defensible in the migration doc as "rerank with
LLM-as-judge", deferrable to a real cross-encoder in the full
submission.

Contract
========

* Input: query + at most :data:`MAX_CANDIDATES` :class:`RetrievedChunk`
  objects from the BM25 stage.
* The judge scores each chunk on a 0..1 relevance scale.
* Output: chunks re-sorted by rerank score, BM25 score broken out as
  a tie-break key, top-k returned.
* Failure modes (Anthropic API error, malformed JSON) are logged and
  fall back to the input order — never raise to the caller. The
  rerank stage is best-effort.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import structlog
from anthropic import Anthropic

from clinical_copilot.corpus.retriever import RetrievedChunk

logger = structlog.get_logger(__name__)

DEFAULT_RERANK_MODEL = "claude-haiku-4-5"
"""Cheap, fast model — judges don't need Sonnet-level reasoning."""

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
) -> list[RetrievedChunk]:
    """Score, sort, and return the top-k chunks.

    Best-effort: any failure (API error, malformed JSON, missing
    chunk_ids) falls back to the input order capped at ``top_k`` so
    the caller never has to handle rerank-stage exceptions.
    """

    if not candidates:
        return []
    if top_k <= 0:
        return []
    candidates_capped = candidates[:MAX_CANDIDATES]
    user_payload = _build_user_message(query=query, chunks=candidates_capped)

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
        return candidates_capped[:top_k]

    text = "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", "") == "text"
    )
    scores_by_id = _parse_scores(text)
    if scores_by_id is None:
        logger.warning("corpus.rerank.parse_failed", text_len=len(text))
        return candidates_capped[:top_k]

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
    return [r.chunk for r in reranked[:top_k]]


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
