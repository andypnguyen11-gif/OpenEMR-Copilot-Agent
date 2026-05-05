"""Tests for the deterministic sentence-window chunker."""

from __future__ import annotations

import pytest

from clinical_copilot.corpus.chunker import chunk_text, split_sentences


def test_split_sentences_handles_multiple_endings() -> None:
    text = "First sentence. Second sentence! Third? Fourth."
    assert split_sentences(text) == [
        "First sentence.",
        "Second sentence!",
        "Third?",
        "Fourth.",
    ]


def test_chunk_text_window_3_stride_1_overlap() -> None:
    text = "One. Two. Three. Four. Five."
    chunks = chunk_text(text=text, source_doc_id="doc-1", window=3, stride=1)
    # 5 sentences, window 3, stride 1 → 3 full-window chunks. Trailing
    # short windows are not emitted: every chunk is exactly window-sized
    # whenever possible, and the last chunk anchors on the final
    # sentence so end-of-doc content is still covered.
    assert [c.text for c in chunks] == [
        "One. Two. Three.",
        "Two. Three. Four.",
        "Three. Four. Five.",
    ]
    # chunk_id is stable + zero-indexed.
    assert chunks[0].chunk_id == "doc-1#0"
    assert chunks[-1].chunk_id == "doc-1#2"


def test_chunk_text_with_window_larger_than_doc_returns_one_chunk() -> None:
    chunks = chunk_text(text="Only one.", source_doc_id="d", window=5, stride=1)
    assert len(chunks) == 1
    assert chunks[0].text == "Only one."


def test_chunk_text_empty_text_returns_no_chunks() -> None:
    assert chunk_text(text="", source_doc_id="d") == []


def test_chunk_text_rejects_zero_window_or_stride() -> None:
    with pytest.raises(ValueError):
        chunk_text(text="x.", source_doc_id="d", window=0)
    with pytest.raises(ValueError):
        chunk_text(text="x.", source_doc_id="d", stride=0)
