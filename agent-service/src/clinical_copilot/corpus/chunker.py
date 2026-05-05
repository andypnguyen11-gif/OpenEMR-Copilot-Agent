"""Sentence-window chunker (W2_ARCHITECTURE §6).

Pure function over a markdown body string. Produces overlapping windows
of consecutive sentences (default window=3, stride=1) so a fact that
spans a sentence boundary still appears intact in at least one chunk.

Sentence splitting is regex-based (period/question/exclamation followed
by whitespace + capital). Good enough for U.S. clinical guidance text;
the production W2-06 chunker can swap to a model-based splitter if the
corpus grows to include heavy abbreviation usage that confuses the
heuristic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Match a sentence-ending punctuation, optional close-paren/quote, then
# whitespace, then a capital letter (negative lookahead would over-split
# on common abbreviations).
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])(?:[\")\]]?)\s+(?=[A-Z(])")
_SENT_END = re.compile(r"[.!?][\")\]]?\s*$")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage."""

    chunk_id: str  # "{source_doc_id}#{chunk_index}"
    source_doc_id: str
    text: str
    sentence_start: int
    sentence_end: int  # exclusive


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences. Whitespace is normalized."""

    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = _SENT_BOUNDARY.split(cleaned)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(
    *,
    text: str,
    source_doc_id: str,
    window: int = 3,
    stride: int = 1,
) -> list[Chunk]:
    """Return overlapping sentence-window chunks for `text`.

    `window=3, stride=1` means every chunk has 3 sentences and the next
    chunk starts 1 sentence later — every sentence except the very
    first appears in 3 consecutive chunks. This is the deterministic
    contract retrieval depends on; do not change defaults without
    rebuilding the index.
    """

    if window < 1 or stride < 1:
        raise ValueError("window and stride must be ≥ 1")

    sentences = split_sentences(text)
    if not sentences:
        return []

    chunks: list[Chunk] = []
    index = 0
    start = 0
    while start < len(sentences):
        end = min(start + window, len(sentences))
        body = " ".join(sentences[start:end])
        chunks.append(
            Chunk(
                chunk_id=f"{source_doc_id}#{index}",
                source_doc_id=source_doc_id,
                text=body,
                sentence_start=start,
                sentence_end=end,
            )
        )
        index += 1
        if end >= len(sentences):
            break
        start += stride

    return chunks
