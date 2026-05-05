"""Hybrid corpus retriever (BM25 + optional dense, RRF-fused).

Loads the artifacts produced by `corpus.index`:

  - `bm25.pkl`  — pickled `(BM25Okapi, list[ChunkRecord])`. Required.
  - `dense.pkl` — pickled `(list[ChunkRecord], np.ndarray)`. Optional.

When both are present **and** an `Embedder` is available for query-
time encoding, retrieval fuses BM25 and dense top-N via Reciprocal
Rank Fusion (RRF, k=60 — the de-facto baseline). When either the
dense file or the embedder is absent, the retriever degrades to
BM25-only — same public surface (`retrieve(query, k)` returning a
list of ranked chunks with citation metadata) so callers don't
care which mode is active.

A pgvector / cross-encoder rerank stage is documented in
W2_ARCHITECTURE §6 as a future swap; the current file-backed
implementation uses a NumPy linear-scan dot product, which is fine
for the corpus sizes we ship (~10-10K chunks).
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from clinical_copilot.corpus.embedder import Embedder, default_embedder
from clinical_copilot.corpus.index import BM25_PATH, DENSE_PATH, tokenize
from clinical_copilot.corpus.records import ChunkRecord

logger = logging.getLogger(__name__)

# RRF constant. 60 is what the original RRF paper used and what
# every implementation since has copied. Tunable but rarely worth it.
_RRF_K: int = 60

# Per-source-list candidate count fed into RRF. Each list contributes
# up to this many ranked chunks before fusion.
_FUSE_TOP_N: int = 20

# Pickle layout invariant: each artifact is a `(metadata, payload)`
# 2-tuple. Centralized so the structural check has a name.
_PICKLE_TUPLE_SIZE: int = 2


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str
    source_doc_id: str
    title: str
    source: str
    source_url: str
    text: str
    score: float


class CorpusRetriever:
    """Loads the corpus indexes once at construction and serves queries."""

    def __init__(
        self,
        *,
        bm25_path: Path | None = None,
        dense_path: Path | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        # ---- BM25 (required) ----
        bm25_path_resolved = bm25_path or BM25_PATH
        if not bm25_path_resolved.exists():
            raise FileNotFoundError(
                f"BM25 index not found at {bm25_path_resolved}. "
                "Run `python -m clinical_copilot.corpus.index --rebuild` first."
            )
        # Pickle is built locally from committed sources, never from
        # untrusted input — so the usual unpickling-RCE concern doesn't
        # apply. Any deviation from the expected (BM25Okapi, list)
        # layout fails fast below.
        with bm25_path_resolved.open("rb") as fh:
            obj = pickle.load(fh)
        if not (isinstance(obj, tuple) and len(obj) == _PICKLE_TUPLE_SIZE):
            raise ValueError(f"Unexpected BM25 pickle layout in {bm25_path_resolved}")
        self._bm25: BM25Okapi = obj[0]
        self._chunks: list[ChunkRecord] = list(obj[1])

        # ---- Dense (optional) ----
        dense_path_resolved = dense_path or DENSE_PATH
        self._dense_chunks: list[ChunkRecord] | None = None
        self._embeddings: np.ndarray | None = None
        if dense_path_resolved.exists():
            with dense_path_resolved.open("rb") as fh:
                dense_obj = pickle.load(fh)
            if not (
                isinstance(dense_obj, tuple) and len(dense_obj) == _PICKLE_TUPLE_SIZE
            ):
                raise ValueError(
                    f"Unexpected dense pickle layout in {dense_path_resolved}"
                )
            dense_chunks_obj, embeddings_obj = dense_obj
            if not isinstance(embeddings_obj, np.ndarray):
                raise ValueError(
                    f"Dense pickle's second element is not an ndarray "
                    f"(got {type(embeddings_obj).__name__})"
                )
            self._dense_chunks = list(dense_chunks_obj)
            self._embeddings = embeddings_obj.astype(np.float32, copy=False)

        # ---- Embedder for query-time encoding ----
        # Only matters if the dense file is present. If embedder is
        # missing while dense is present, we degrade to BM25-only and
        # log it (so a misconfigured deployment is visible).
        self._embedder: Embedder | None
        if self._embeddings is None:
            self._embedder = None
        elif embedder is not None:
            self._embedder = embedder
        else:
            self._embedder = default_embedder()
            if self._embedder is None:
                logger.warning(
                    "Dense index file %s exists but no embedder is configured; "
                    "set OPENAI_API_KEY or pass embedder=... to enable hybrid "
                    "retrieval. Falling back to BM25-only.",
                    dense_path_resolved,
                )
                # Drop the dense state so the fallback is consistent.
                self._embeddings = None
                self._dense_chunks = None

    @property
    def hybrid_enabled(self) -> bool:
        """True when both the dense index and an embedder are loaded."""
        return self._embeddings is not None and self._embedder is not None

    def retrieve(self, query: str, *, k: int = 5) -> list[RetrievedChunk]:
        if k <= 0:
            return []
        bm25_ranked = self._bm25_rank(query, top_n=max(k, _FUSE_TOP_N))
        if not self.hybrid_enabled:
            return bm25_ranked[:k]
        dense_ranked = self._dense_rank(query, top_n=max(k, _FUSE_TOP_N))
        return self._fuse_rrf(bm25_ranked, dense_ranked, k=k)

    # ----- BM25 -----

    def _bm25_rank(self, query: str, *, top_n: int) -> list[RetrievedChunk]:
        tokens = tokenize(query)
        if not tokens:
            return []
        # BM25Okapi's IDF can be ≤ 0 on small corpora (a term that
        # appears in every document scores zero or negative), so we
        # cannot threshold on score alone. Drop chunks whose token set
        # has no overlap with the query — that's the clean "no
        # semantic relevance possible" filter and is independent of
        # corpus size.
        query_set = set(tokens)
        scores = self._bm25.get_scores(tokens)
        ranked_indices = sorted(
            range(len(scores)), key=lambda i: float(scores[i]), reverse=True
        )
        out: list[RetrievedChunk] = []
        for idx in ranked_indices:
            if len(out) >= top_n:
                break
            rec = self._chunks[idx]
            chunk_tokens = set(tokenize(rec.text))
            if not (query_set & chunk_tokens):
                continue
            out.append(_to_retrieved(rec, score=float(scores[idx])))
        return out

    # ----- Dense -----

    def _dense_rank(self, query: str, *, top_n: int) -> list[RetrievedChunk]:
        # Only called when hybrid_enabled is True.
        assert self._embeddings is not None
        assert self._embedder is not None
        assert self._dense_chunks is not None

        q = self._embedder.embed_batch([query])
        if q.shape[0] != 1:
            return []
        # `embed_batch` is contracted to L2-normalize. The stored
        # embeddings are also normalized at index time. So a dot
        # product is cosine similarity.
        scores = (self._embeddings @ q[0]).astype(np.float32, copy=False)
        if scores.size == 0:
            return []
        # Use argpartition for top-N, then sort just those.
        n = min(top_n, scores.shape[0])
        partition = np.argpartition(-scores, n - 1)[:n]
        ordered = partition[np.argsort(-scores[partition])]
        return [
            _to_retrieved(self._dense_chunks[int(i)], score=float(scores[int(i)]))
            for i in ordered
        ]

    # ----- Fusion -----

    @staticmethod
    def _fuse_rrf(
        bm25_ranked: list[RetrievedChunk],
        dense_ranked: list[RetrievedChunk],
        *,
        k: int,
        k_rrf: int = _RRF_K,
    ) -> list[RetrievedChunk]:
        """Reciprocal Rank Fusion. Each list contributes
        ``1 / (k_rrf + rank)`` per result; per-chunk RRF scores are
        summed across lists; the top-`k` by fused score wins.

        RRF is rank-based, so we don't need to normalize BM25 and
        cosine onto the same scale — that's the whole point.
        """
        scores: dict[str, float] = {}
        canonical: dict[str, RetrievedChunk] = {}
        for rank, c in enumerate(bm25_ranked):
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + 1.0 / (k_rrf + rank + 1)
            canonical.setdefault(c.chunk_id, c)
        for rank, c in enumerate(dense_ranked):
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + 1.0 / (k_rrf + rank + 1)
            canonical.setdefault(c.chunk_id, c)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [
            RetrievedChunk(
                chunk_id=cid,
                source_doc_id=canonical[cid].source_doc_id,
                title=canonical[cid].title,
                source=canonical[cid].source,
                source_url=canonical[cid].source_url,
                text=canonical[cid].text,
                score=float(rrf_score),
            )
            for cid, rrf_score in ordered
        ]


def _to_retrieved(rec: ChunkRecord, *, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=rec.chunk_id,
        source_doc_id=rec.source_doc_id,
        title=rec.title,
        source=rec.source,
        source_url=rec.source_url,
        text=rec.text,
        score=score,
    )
