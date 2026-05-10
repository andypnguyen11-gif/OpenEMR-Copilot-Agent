"""Unit tests for the rendered-page disk cache.

The cache is the citation-overlay UI's only source of page images: the
OpenEMR docker image has no PDF rasterizer, so the agent-service ingest
path renders once at extraction time and the bbox-overlay route reads
from this cache. The contract these tests pin:

* Bytes round-trip exactly. PNG content matters byte-for-byte for the
  overlay JS (it measures the rendered image's intrinsic dimensions to
  scale the bbox coordinates).
* ``read`` returns ``None`` on miss — the route layer turns that into a
  structured 404 (the "PHP must not have to guess" guard captured in
  PR 5's design).
* ``page_count`` distinguishes "document never rendered" (0) from "page
  index out of range" (>0), which is what the route uses to pick the
  right ``reason`` field on the 404 body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clinical_copilot.documents import page_cache


def test_write_then_read_round_trips_bytes(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    page_cache.write("doc-1", 1, payload, root=tmp_path)
    assert page_cache.read("doc-1", 1, root=tmp_path) == payload


def test_read_unknown_document_returns_none(tmp_path: Path) -> None:
    assert page_cache.read("never-written", 1, root=tmp_path) is None


def test_read_unknown_page_returns_none(tmp_path: Path) -> None:
    page_cache.write("doc-2", 1, b"page-one", root=tmp_path)
    assert page_cache.read("doc-2", 2, root=tmp_path) is None


def test_write_overwrites_existing_page(tmp_path: Path) -> None:
    page_cache.write("doc-3", 1, b"first", root=tmp_path)
    page_cache.write("doc-3", 1, b"second", root=tmp_path)
    assert page_cache.read("doc-3", 1, root=tmp_path) == b"second"


def test_has_reflects_presence(tmp_path: Path) -> None:
    assert page_cache.has("doc-4", 1, root=tmp_path) is False
    page_cache.write("doc-4", 1, b"x", root=tmp_path)
    assert page_cache.has("doc-4", 1, root=tmp_path) is True


def test_page_count_zero_for_unknown_document(tmp_path: Path) -> None:
    assert page_cache.page_count("never-written", root=tmp_path) == 0


def test_page_count_matches_written_pages(tmp_path: Path) -> None:
    page_cache.write("doc-5", 1, b"a", root=tmp_path)
    page_cache.write("doc-5", 2, b"b", root=tmp_path)
    page_cache.write("doc-5", 3, b"c", root=tmp_path)
    assert page_cache.page_count("doc-5", root=tmp_path) == 3


def test_write_rejects_zero_or_negative_page(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        page_cache.write("doc-6", 0, b"x", root=tmp_path)
    with pytest.raises(ValueError):
        page_cache.write("doc-6", -1, b"x", root=tmp_path)


def test_documents_are_isolated(tmp_path: Path) -> None:
    page_cache.write("doc-a", 1, b"alpha", root=tmp_path)
    page_cache.write("doc-b", 1, b"beta", root=tmp_path)
    assert page_cache.read("doc-a", 1, root=tmp_path) == b"alpha"
    assert page_cache.read("doc-b", 1, root=tmp_path) == b"beta"
    assert page_cache.page_count("doc-a", root=tmp_path) == 1
    assert page_cache.page_count("doc-b", root=tmp_path) == 1
