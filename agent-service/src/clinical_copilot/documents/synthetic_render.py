"""Synthetic text-on-page renderer for non-rasterizable formats.

HL7 v2 messages, DOCX referrals, and XLSX workbooks have no native
"page" representation that the citation-overlay UI can show alongside
extracted facts. Without a backing image, the bbox-overlay route
returns 404 and the review form falls back to a "source preview not
available" notice — a regression in the click-to-source UX for any
non-PDF/image upload.

This module synthesizes a deterministic monospace-text page image
from a list of pre-formatted lines. The companion
:func:`compute_line_bbox` produces the normalized bbox for a given
line index using the same layout constants the renderer uses, so an
extractor that knows its citation's source line can publish a bbox
that lines up with the rendered image without ever loading the image
itself. That separation keeps the extractor pure-data and the
renderer pure-pixels — they share constants, not state.

Layout choices, all overridable per format if needed later:

* Page width is fixed at :data:`SYNTH_PAGE_WIDTH_PX`. Lines longer
  than that are not wrapped; they are truncated with an ellipsis.
  The bbox still covers the full line region — the overlay's job is
  to point at the row, not reproduce the wrapped value.
* Page height grows with content, but never falls below
  :data:`SYNTH_MIN_PAGE_HEIGHT_PX` so a 3-segment HL7 doesn't
  render as a postage-stamp image inside the review panel.
* Font is the bundled DejaVu Sans Mono shipped with Pillow when
  available. Falling back to the bitmap default is acceptable for
  OCR-tightening (the tightener doesn't rely on glyph fidelity, just
  on tokens being recognizable to Tesseract).
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# Page width in pixels. ~6 inches at 300 DPI; wide enough to fit a
# typical HL7 segment (200-300 chars) at a comfortable monospace size.
SYNTH_PAGE_WIDTH_PX: int = 1800

# Per-line height in pixels. Sized so a 9pt-equivalent monospace font
# (which renders at ~22 px tall at 300 DPI) leaves ~10 px of vertical
# breathing room above and below the glyphs — enough that Tesseract
# can segment adjacent lines as separate rows.
SYNTH_LINE_HEIGHT_PX: int = 36

# Font size for the monospace glyphs themselves. Slightly under
# SYNTH_LINE_HEIGHT_PX to leave the breathing room described above.
SYNTH_FONT_SIZE_PX: int = 22

# Symmetric page margins. Left/right matter for the line-clip
# calculation; top/bottom keep the first/last line off the image
# edge so the overlay's hover highlight has a frame around it.
SYNTH_TOP_MARGIN_PX: int = 60
SYNTH_BOTTOM_MARGIN_PX: int = 60
SYNTH_LEFT_MARGIN_PX: int = 60
SYNTH_RIGHT_MARGIN_PX: int = 60

# Floor on page height — a 1-line document still gets a reasonably-
# proportioned page so the citation-overlay panel doesn't render a
# postage stamp.
SYNTH_MIN_PAGE_HEIGHT_PX: int = 2400

# Font search list. Pillow ships DejaVuSansMono with the standard
# distribution; on systems where it's missing (minimal containers) we
# fall back to the platform mono font, then to Pillow's bitmap
# default. The bitmap default loses kerning info but Tesseract still
# segments glyphs cleanly.
_FONT_CANDIDATES: tuple[str, ...] = (
    "DejaVuSansMono.ttf",
    "/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "Courier New.ttf",
)


@dataclass(frozen=True, slots=True)
class LayoutMetrics:
    """Resolved page dimensions for a synthetic render.

    Returned alongside :func:`compute_line_bbox` so callers that need
    pixel coordinates (e.g. tests, the renderer itself) can read the
    same numbers the bbox math uses without re-deriving them.
    """

    line_count: int
    page_width_px: int
    page_height_px: int
    line_height_px: int
    top_margin_px: int


def page_metrics(line_count: int) -> LayoutMetrics:
    """Resolve the dimensions of the synthetic page that will hold
    ``line_count`` lines.

    Page height is the larger of:
      * top margin + ``line_count * line_height`` + bottom margin
      * :data:`SYNTH_MIN_PAGE_HEIGHT_PX`

    Treating ``line_count == 0`` as "still produce the minimum page"
    keeps the renderer from raising on an empty source — the resulting
    image is mostly whitespace, but the overlay still has something to
    show.
    """

    safe_count = max(0, line_count)
    natural_height = (
        SYNTH_TOP_MARGIN_PX
        + safe_count * SYNTH_LINE_HEIGHT_PX
        + SYNTH_BOTTOM_MARGIN_PX
    )
    height = max(SYNTH_MIN_PAGE_HEIGHT_PX, natural_height)
    return LayoutMetrics(
        line_count=safe_count,
        page_width_px=SYNTH_PAGE_WIDTH_PX,
        page_height_px=height,
        line_height_px=SYNTH_LINE_HEIGHT_PX,
        top_margin_px=SYNTH_TOP_MARGIN_PX,
    )


def compute_line_bbox(
    line_index: int,
    total_lines: int,
) -> tuple[float, float, float, float]:
    """Return the normalized ``(x0, y0, x1, y1)`` bbox for line
    ``line_index`` (0-based) in a synthetic render of ``total_lines``
    lines.

    X spans the full text width (left margin → right margin)
    normalized; the bbox is row-shaped because no extractor that
    targets this renderer carries column information. Y is the line's
    pixel range (``top_margin + line_index * line_height`` to
    ``top_margin + (line_index + 1) * line_height``) divided by the
    resolved page height.

    Out-of-range ``line_index`` values are clamped to ``[0,
    total_lines - 1]`` rather than raising, since the extractor and
    the renderer agree on ``total_lines`` and a mismatch here would
    indicate a bug at the call site that should not bubble through
    a normalize-and-render pipeline.
    """

    if total_lines <= 0:
        return (0.0, 0.0, 1.0, 1.0)

    safe_index = max(0, min(line_index, total_lines - 1))
    metrics = page_metrics(total_lines)
    line_top_px = SYNTH_TOP_MARGIN_PX + safe_index * SYNTH_LINE_HEIGHT_PX
    line_bottom_px = line_top_px + SYNTH_LINE_HEIGHT_PX
    x0 = SYNTH_LEFT_MARGIN_PX / metrics.page_width_px
    x1 = (metrics.page_width_px - SYNTH_RIGHT_MARGIN_PX) / metrics.page_width_px
    y0 = line_top_px / metrics.page_height_px
    y1 = line_bottom_px / metrics.page_height_px
    return (x0, y0, x1, y1)


def render_lines(lines: list[str]) -> Image.Image:
    """Render ``lines`` onto a single white-background page image.

    The returned image's dimensions exactly match :func:`page_metrics`
    for ``len(lines)``. Each line is drawn at its row's top y
    coordinate (top_margin + i * line_height) plus a small baseline
    offset so glyph ascenders sit centered within the row band — the
    same band that :func:`compute_line_bbox` returns for that index.

    Lines wider than the available text width are visually truncated
    with an ellipsis. The bbox still covers the full line band, so a
    citation pointing at a long HL7 segment lights up the row even
    when the on-screen text is clipped.
    """

    metrics = page_metrics(len(lines))
    image = Image.new(
        "RGB",
        (metrics.page_width_px, metrics.page_height_px),
        color="white",
    )
    if not lines:
        return image

    draw = ImageDraw.Draw(image)
    font = _load_mono_font()
    text_width_px = (
        metrics.page_width_px - SYNTH_LEFT_MARGIN_PX - SYNTH_RIGHT_MARGIN_PX
    )

    for i, raw_line in enumerate(lines):
        line_top_px = SYNTH_TOP_MARGIN_PX + i * SYNTH_LINE_HEIGHT_PX
        # Center the glyph baseline inside the row band. Pillow's
        # default text anchor draws from the top-left of the bounding
        # box, so half the leftover row height becomes top padding.
        glyph_top_px = line_top_px + (SYNTH_LINE_HEIGHT_PX - SYNTH_FONT_SIZE_PX) // 2
        text = _truncate_to_fit(raw_line, font, text_width_px, draw)
        draw.text(
            (SYNTH_LEFT_MARGIN_PX, glyph_top_px),
            text,
            fill="black",
            font=font,
        )
    return image


def _load_mono_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, SYNTH_FONT_SIZE_PX)
        except OSError:
            continue
    # Bitmap default. Pillow returns a ~10px tall font here, smaller
    # than SYNTH_FONT_SIZE_PX, but row positioning still works because
    # compute_line_bbox uses the row band, not the glyph bounds.
    return ImageFont.load_default()


def _truncate_to_fit(
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    max_width_px: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """If ``text`` is wider than ``max_width_px``, return a prefix
    that fits with a trailing ``...`` ellipsis. Returns ``text``
    unchanged when it already fits."""

    if _measure_width(text, font, draw) <= max_width_px:
        return text
    ellipsis = "..."
    ellipsis_width = _measure_width(ellipsis, font, draw)
    if ellipsis_width >= max_width_px:
        return ellipsis  # absurdly small page; degrade gracefully
    # Binary-search the longest prefix that fits with the ellipsis.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid]
        if _measure_width(candidate, font, draw) + ellipsis_width <= max_width_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def _measure_width(
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    draw: ImageDraw.ImageDraw,
) -> int:
    """Return the rendered pixel width of ``text`` in ``font``."""

    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]
