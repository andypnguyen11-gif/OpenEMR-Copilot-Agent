"""Pickled-stable record types for the corpus pipeline.

`corpus.index` runs as a module (`python -m clinical_copilot.corpus.index`)
which makes its dataclasses live under `__main__` at index-build time.
Pickle stores the class qualname under whatever module the class was
defined in at *write* time, so re-loading a pickle written from the
index CLI fails when the reader process looks the class up on its own
`__main__` module instead.

Putting the pickled records here — in a module that is never the
script entrypoint — gives them a stable module path the reader can
always resolve.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """What the BM25 index returns alongside a score."""

    chunk_id: str
    source_doc_id: str
    text: str
    title: str
    source: str
    source_url: str
