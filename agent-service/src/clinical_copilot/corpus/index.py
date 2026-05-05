"""Corpus indexer CLI (W2-06 — BM25 + optional dense, file-backed).

Reads `corpus/sources/**/*.md`, parses YAML frontmatter, chunks each
document, runs the PHI-shape scrub, builds:

    data/corpus/bm25.pkl       — pickled (BM25Okapi, list[ChunkRecord])
    data/corpus/dense.pkl      — pickled (list[ChunkRecord], np.ndarray)  [optional]
    data/corpus/manifest.json  — per-source-doc + per-chunk metadata

The dense path is **optional**. If no embedder is configured (e.g.
`OPENAI_API_KEY` is unset), the indexer logs a note and produces
BM25-only output. The retriever picks up whichever artifacts exist
and does hybrid retrieval if both are present, BM25-only otherwise.
This keeps the indexer runnable on a fresh checkout / in CI without
provisioning an API key.

A pgvector / cross-encoder rerank stage is documented in
W2_ARCHITECTURE §6 as a future swap; the file-backed implementation
here lives in `data/corpus/dense.pkl` and is read via NumPy linear
scan — fine through ~10K chunks given our corpus size.

Usage::

    uv run python -m clinical_copilot.corpus.index --rebuild
    uv run python -m clinical_copilot.corpus.index --rebuild --bm25-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pickle
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import yaml
from rank_bm25 import BM25Okapi

from clinical_copilot.corpus.chunker import Chunk, chunk_text
from clinical_copilot.corpus.embedder import Embedder, default_embedder
from clinical_copilot.corpus.records import ChunkRecord
from clinical_copilot.corpus.scrub import scrub_or_raise

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
SOURCES_DIR = ROOT / "corpus" / "sources"
DATA_DIR = ROOT / "data" / "corpus"
BM25_PATH = DATA_DIR / "bm25.pkl"
DENSE_PATH = DATA_DIR / "dense.pkl"
MANIFEST_PATH = DATA_DIR / "manifest.json"


@dataclass(frozen=True, slots=True)
class SourceDoc:
    doc_id: str  # path relative to corpus/sources, no extension
    title: str
    source: str
    source_url: str
    date: str
    topics: list[str]
    body: str
    sha256: str


def load_sources(sources_dir: Path = SOURCES_DIR) -> list[SourceDoc]:
    docs: list[SourceDoc] = []
    for path in sorted(sources_dir.rglob("*.md")):
        if path.name == "LICENSES.md":
            continue
        text = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        rel = path.relative_to(sources_dir).with_suffix("")
        doc_id = str(rel).replace("\\", "/")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        docs.append(
            SourceDoc(
                doc_id=doc_id,
                title=_require_str(meta, "title", doc_id),
                source=_require_str(meta, "source", doc_id),
                source_url=_require_str(meta, "source_url", doc_id),
                date=_coerce_str(meta.get("date", "")),
                topics=_coerce_str_list(meta.get("topics")),
                body=body,
                sha256=sha,
            )
        )
    return docs


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    # Splitting on "---" with maxsplit=2 yields three parts: leading
    # empty (before first ---), the YAML, the body (after closing ---).
    expected_parts = 3
    if not text.startswith("---"):
        raise ValueError("source doc is missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < expected_parts:
        raise ValueError("malformed YAML frontmatter (missing closing ---)")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    body = parts[2].strip()
    return meta, body


def _require_str(meta: dict[str, object], key: str, doc_id: str) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"frontmatter for {doc_id!r} is missing required string {key!r}")
    return value


def _coerce_str(value: object) -> str:
    # YAML parses bare ISO dates as datetime.date. Normalize anything
    # non-string by str() so the frontmatter author can write `date:
    # 2025-01-01` without quoting.
    if isinstance(value, str):
        return value
    return str(value) if value is not None else ""


def _coerce_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"expected a YAML list for topics, got {type(value).__name__}")
    return [str(item) for item in value]


def build_chunks(docs: list[SourceDoc]) -> list[tuple[Chunk, SourceDoc]]:
    out: list[tuple[Chunk, SourceDoc]] = []
    for doc in docs:
        scrub_or_raise(source_doc_id=doc.doc_id, text=doc.body)
        for chunk in chunk_text(text=doc.body, source_doc_id=doc.doc_id):
            out.append((chunk, doc))
    return out


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]+")


def tokenize(text: str) -> list[str]:
    """Lowercase BM25 tokenizer. Not a sophisticated NLP pipeline —
    BM25 cares about token presence and frequency, not lemmas."""

    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


_SENTINEL: object = object()


def build_index(
    *,
    sources_dir: Path = SOURCES_DIR,
    data_dir: Path = DATA_DIR,
    embedder: Embedder | None | object = _SENTINEL,
) -> dict[str, object]:
    """Build BM25 + (optionally) dense indexes from `sources_dir`.

    `embedder` semantics:
      - omitted (default): call `default_embedder()`; build dense if
        an embedder is configured, skip the dense path otherwise.
      - explicit `None`: skip the dense path unconditionally
        (equivalent to the CLI `--bm25-only` flag; useful in tests).
      - an `Embedder`: use it.
    """
    docs = load_sources(sources_dir)
    paired = build_chunks(docs)

    chunk_records = [
        ChunkRecord(
            chunk_id=chunk.chunk_id,
            source_doc_id=chunk.source_doc_id,
            text=chunk.text,
            title=doc.title,
            source=doc.source,
            source_url=doc.source_url,
        )
        for chunk, doc in paired
    ]
    tokens = [tokenize(rec.text) for rec in chunk_records]
    bm25 = BM25Okapi(tokens)

    data_dir.mkdir(parents=True, exist_ok=True)
    bm25_path = data_dir / "bm25.pkl"
    with bm25_path.open("wb") as fh:
        pickle.dump((bm25, chunk_records), fh)

    # Resolve the embedder. Sentinel means "auto-detect"; explicit
    # None means "skip dense path" (CLI --bm25-only or tests).
    resolved_embedder: Embedder | None
    if embedder is _SENTINEL:
        resolved_embedder = default_embedder()
        if resolved_embedder is None:
            logger.info(
                "No embedder configured (set OPENAI_API_KEY to enable). "
                "Indexer is producing BM25-only artifacts; retriever will "
                "degrade to BM25-only retrieval at query time."
            )
    else:
        # mypy: narrowed to Embedder | None by the sentinel branch above.
        resolved_embedder = embedder  # type: ignore[assignment]

    dense_path: Path | None = None
    embedding_dim: int | None = None
    if resolved_embedder is not None and chunk_records:
        embeddings = resolved_embedder.embed_batch([rec.text for rec in chunk_records])
        if embeddings.shape[0] != len(chunk_records):
            raise RuntimeError(
                f"Embedder returned {embeddings.shape[0]} vectors for "
                f"{len(chunk_records)} chunks"
            )
        dense_path = data_dir / "dense.pkl"
        with dense_path.open("wb") as fh:
            pickle.dump((chunk_records, embeddings.astype(np.float32)), fh)
        embedding_dim = int(embeddings.shape[1])

    manifest = {
        "version": 1,
        "doc_count": len(docs),
        "chunk_count": len(chunk_records),
        "permitted_sources": sorted({doc.source for doc in docs}),
        "dense_built": dense_path is not None,
        "embedding_dim": embedding_dim,
        "documents": [
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "source": doc.source,
                "source_url": doc.source_url,
                "date": doc.date,
                "topics": doc.topics,
                "sha256": doc.sha256,
            }
            for doc in docs
        ],
        "chunks": [asdict(rec) for rec in chunk_records],
    }
    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "bm25_path": str(bm25_path),
        "dense_path": str(dense_path) if dense_path is not None else None,
        "manifest_path": str(manifest_path),
        "doc_count": len(docs),
        "chunk_count": len(chunk_records),
        "embedding_dim": embedding_dim,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="corpus.index")
    p.add_argument("--rebuild", action="store_true", help="(default) Rebuild from sources.")
    p.add_argument(
        "--sources",
        type=Path,
        default=SOURCES_DIR,
        help="Override the sources directory.",
    )
    p.add_argument(
        "--data",
        type=Path,
        default=DATA_DIR,
        help="Override the output data directory.",
    )
    p.add_argument(
        "--bm25-only",
        action="store_true",
        help="Skip the dense embedding pass even if an embedder is "
        "configured. Default behaviour auto-detects via "
        "OPENAI_API_KEY.",
    )
    args = p.parse_args(argv)

    embedder_arg: Embedder | None | object = None if args.bm25_only else _SENTINEL
    result = build_index(
        sources_dir=args.sources,
        data_dir=args.data,
        embedder=embedder_arg,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
