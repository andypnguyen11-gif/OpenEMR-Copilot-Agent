"""Tests for the synthetic-text page renderer.

Covers the layout-math contract that the format-specific extractors
(HL7, docx, xlsx) rely on to publish bboxes that line up with the
rendered image. The renderer-side image production is exercised
through :mod:`test_fetcher_synthetic` to keep the I/O-heavy paths
out of this fast unit-test file.
"""

from __future__ import annotations

import pytest
from PIL import Image

from clinical_copilot.documents.synthetic_render import (
    SYNTH_LINE_HEIGHT_PX,
    SYNTH_MIN_PAGE_HEIGHT_PX,
    SYNTH_PAGE_WIDTH_PX,
    SYNTH_TOP_MARGIN_PX,
    compute_line_bbox,
    page_metrics,
    render_lines,
)


def test_page_metrics_returns_min_height_for_short_documents() -> None:
    metrics = page_metrics(line_count=3)
    assert metrics.page_width_px == SYNTH_PAGE_WIDTH_PX
    # 3 lines * 36 px + 60 px top + 60 px bottom = 228 px, well under
    # the 2400 floor — so the page should be the floor.
    assert metrics.page_height_px == SYNTH_MIN_PAGE_HEIGHT_PX


def test_page_metrics_grows_with_long_documents() -> None:
    # A line count high enough that natural height exceeds the floor.
    line_count = (SYNTH_MIN_PAGE_HEIGHT_PX // SYNTH_LINE_HEIGHT_PX) + 5
    metrics = page_metrics(line_count=line_count)
    assert metrics.page_height_px > SYNTH_MIN_PAGE_HEIGHT_PX
    expected = SYNTH_TOP_MARGIN_PX + line_count * SYNTH_LINE_HEIGHT_PX + 60
    assert metrics.page_height_px == expected


def test_page_metrics_handles_empty_input() -> None:
    """A zero-line input must still produce a valid page so the
    upstream renderer doesn't divide by zero or raise. The page is
    mostly whitespace; the citation overlay treats it the same as a
    cache-miss placeholder."""

    metrics = page_metrics(line_count=0)
    assert metrics.page_width_px == SYNTH_PAGE_WIDTH_PX
    assert metrics.page_height_px == SYNTH_MIN_PAGE_HEIGHT_PX


def test_compute_line_bbox_returns_full_page_for_empty_total() -> None:
    """Defensive path: when the extractor passes ``total_lines == 0``
    (only possible for a corrupted source), return a full-page bbox
    rather than dividing by zero. The overlay still draws something.
    """

    bbox = compute_line_bbox(line_index=0, total_lines=0)
    assert bbox == (0.0, 0.0, 1.0, 1.0)


def test_compute_line_bbox_clamps_out_of_range_index() -> None:
    """If the extractor's line bookkeeping diverges from the
    renderer's by one, we'd rather draw the bbox of the last line
    than raise — the bug should still be fixed at the source, but a
    panel that never throws is more useful in production than one
    that 500s on stale data.
    """

    bbox_low = compute_line_bbox(line_index=-2, total_lines=4)
    bbox_high = compute_line_bbox(line_index=99, total_lines=4)
    bbox_first = compute_line_bbox(line_index=0, total_lines=4)
    bbox_last = compute_line_bbox(line_index=3, total_lines=4)
    assert bbox_low == bbox_first
    assert bbox_high == bbox_last


def test_compute_line_bbox_lines_are_non_overlapping_and_ordered() -> None:
    total = 12
    bboxes = [compute_line_bbox(i, total) for i in range(total)]
    for i in range(total - 1):
        # Each line's bottom is the next line's top — no overlap, no
        # gap, since the renderer draws contiguous row bands.
        assert bboxes[i][3] == pytest.approx(bboxes[i + 1][1])
    # All y-values are within [0, 1].
    for x0, y0, x1, y1 in bboxes:
        assert 0.0 <= y0 < y1 <= 1.0
        assert 0.0 <= x0 < x1 <= 1.0


def test_compute_line_bbox_all_lines_share_x_range() -> None:
    """The renderer's text band has fixed left/right margins, so
    every line's bbox should occupy the same horizontal extent.
    Citations target a row, never a column."""

    total = 5
    x_ranges = {(b[0], b[2]) for b in (compute_line_bbox(i, total) for i in range(total))}
    assert len(x_ranges) == 1


def test_render_lines_produces_image_matching_page_metrics() -> None:
    lines = ["ALPHA", "BETA", "GAMMA"]
    image = render_lines(lines)
    metrics = page_metrics(len(lines))
    assert image.size == (metrics.page_width_px, metrics.page_height_px)
    assert isinstance(image, Image.Image)


def test_render_lines_handles_empty_input() -> None:
    """An empty-source render should still be a valid white page so
    the page-cache route doesn't 404 the overlay just because the
    file had no parseable content."""

    image = render_lines([])
    metrics = page_metrics(0)
    assert image.size == (metrics.page_width_px, metrics.page_height_px)


def test_render_lines_truncates_overlong_text_with_ellipsis() -> None:
    """Lines wider than the available text width should be drawn
    with a trailing ellipsis. We can't easily inspect the rendered
    text directly without OCR, so the assertion is structural: the
    render does not raise for arbitrarily long input.
    """

    huge = "X" * 10_000
    image = render_lines([huge, "short line"])
    metrics = page_metrics(2)
    assert image.size == (metrics.page_width_px, metrics.page_height_px)
