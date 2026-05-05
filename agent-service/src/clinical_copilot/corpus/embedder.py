"""Embedder abstraction for dense corpus indexing.

The corpus indexer needs to map chunk text → vector. We keep this
behind a `Protocol` so the choice of embedding service stays out of
the indexer's hot path. Default implementation: OpenAI's
`text-embedding-3-small` (1536 dims), matching PRD2 §10.

Configure via `OPENAI_API_KEY`. If unset, callers should treat the
absence as "skip the dense path" rather than fail — the indexer
emits BM25-only when no embedder is available, and the retriever
degrades to BM25-only retrieval. This keeps `python -m
clinical_copilot.corpus.index --rebuild` working in CI / on a
fresh checkout without provisioning a key.

Vectors returned by every implementation **must be L2-normalized**
so cosine similarity reduces to a dot product downstream. The
indexer and retriever both rely on that invariant.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import numpy as np

EMBEDDING_DIM = 1536  # text-embedding-3-small


class EmbedderUnavailable(RuntimeError):
    """No embedder is configured.

    Callers should treat this as a soft-skip on the dense path,
    not a fatal error. `default_embedder()` catches it.
    """


@runtime_checkable
class Embedder(Protocol):
    """Maps a batch of strings to a `(N, D)` float32 matrix.

    The returned vectors must be L2-normalized — cosine similarity
    elsewhere is computed as a dot product, so unnormalized vectors
    silently break ranking.
    """

    @property
    def dim(self) -> int: ...

    def embed_batch(self, texts: list[str]) -> np.ndarray: ...


class OpenAIEmbedder:
    """`text-embedding-3-small` via the OpenAI API."""

    dim: int = EMBEDDING_DIM
    _MODEL: str = "text-embedding-3-small"
    _BATCH: int = 100

    def __init__(self, *, api_key: str | None = None) -> None:
        try:
            # Lazy import: keeps `corpus` importable in environments
            # where `openai` is not installed; the indexer / retriever
            # treat that as "skip the dense path" via EmbedderUnavailable.
            from openai import OpenAI  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — exercised only when dep absent
            raise EmbedderUnavailable(
                "openai package is not installed; cannot use OpenAI embedder"
            ) from exc

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise EmbedderUnavailable(
                "OPENAI_API_KEY is not set; cannot use OpenAI embedder"
            )
        self._client = OpenAI(api_key=key)

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self._BATCH):
            batch = texts[i : i + self._BATCH]
            resp = self._client.embeddings.create(model=self._MODEL, input=batch)
            out.append(np.asarray([d.embedding for d in resp.data], dtype=np.float32))
        m = np.vstack(out)
        return _l2_normalize(m)


def _l2_normalize(m: np.ndarray) -> np.ndarray:
    """L2-normalize each row. Zero-norm rows are returned unchanged
    (they'll dot-product to 0, which is the correct similarity)."""

    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return (m / norms).astype(np.float32)


def default_embedder() -> Embedder | None:
    """Return the configured embedder, or `None` if unavailable.

    Index and retriever both call this and degrade to BM25-only
    when `None`. Tests should pass an explicit mock instead of
    relying on this default.
    """
    try:
        return OpenAIEmbedder()
    except EmbedderUnavailable:
        return None


__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedderUnavailable",
    "OpenAIEmbedder",
    "_l2_normalize",
    "default_embedder",
]
