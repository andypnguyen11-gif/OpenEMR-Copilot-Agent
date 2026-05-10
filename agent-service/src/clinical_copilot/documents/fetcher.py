"""Document page rendering for the demo extractor (W2-03, demo cut).

The full W2-03 fetcher also pulls the Binary via FHIR with a
system-scoped JWT; this demo cut reads from a local path. Both code
paths produce the same `RenderedPage` so the rest of the extractor
does not have to know which produced it.

Supports PDFs (rendered via pypdfium2) and single-image documents
(PNG / JPG / JPEG / TIFF) loaded directly. The two paths return the
same `RenderedPage` shape so the extractor doesn't branch on format.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

# Render at 300 DPI — high enough for OCR-quality citation checks
# downstream, low enough that a 5-page lab report stays under the
# Anthropic vision per-image size budget after JPEG compression.
RENDER_DPI: int = 300

# Anthropic's vision endpoint accepts images up to 8000 px on the long
# edge. Large scanner outputs (2400+ DPI scans) blow past that; cap
# the long edge here so a 4000 px scan downsamples to something the
# API accepts without the extractor having to know about it.
MAX_LONG_EDGE_PX: int = 2400

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})


@dataclass(frozen=True, slots=True)
class RenderedPage:
    page_number: int  # 1-indexed
    image: Image.Image
    width_px: int
    height_px: int


def render_document(path: Path) -> list[RenderedPage]:
    """Render every page of `path` to a PIL.Image.

    PDFs go through pypdfium2 at `RENDER_DPI`. Single-image documents
    (PNG, JPG, JPEG, BMP, WEBP) are loaded directly and returned as a
    one-page list. Multi-page TIFFs (fax packets — one of the cohort-5
    file formats) are iterated via Pillow's ``n_frames`` so each page
    becomes its own ``RenderedPage``. Unknown extensions fall back to
    the image loader — most scanners produce one of the listed formats.
    """

    if not path.exists():
        raise FileNotFoundError(f"document not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _render_pdf(path)
    if suffix in {".tif", ".tiff"}:
        return _render_tiff(path)
    if suffix in _IMAGE_EXTS:
        return _render_image(path)
    raise ValueError(
        f"Unsupported document extension {suffix!r} for {path.name}. "
        f"Supported: .pdf and {sorted(_IMAGE_EXTS)}."
    )


# Backwards-compat alias — earlier W2-01/03 code imported `render_pdf`.
render_pdf = render_document


def _render_pdf(path: Path) -> list[RenderedPage]:
    pages: list[RenderedPage] = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for index, page in enumerate(pdf):
            scale = RENDER_DPI / 72.0
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil().convert("RGB")
            pil_image = _cap_long_edge(pil_image)
            pages.append(
                RenderedPage(
                    page_number=index + 1,
                    image=pil_image,
                    width_px=pil_image.width,
                    height_px=pil_image.height,
                )
            )
    finally:
        pdf.close()
    return pages


def _render_image(path: Path) -> list[RenderedPage]:
    image = Image.open(path).convert("RGB")
    image = _cap_long_edge(image)
    return [
        RenderedPage(
            page_number=1,
            image=image,
            width_px=image.width,
            height_px=image.height,
        )
    ]


def _render_tiff(path: Path) -> list[RenderedPage]:
    """Iterate every frame of a (possibly multi-page) TIFF.

    Fax-packet TIFFs in the cohort-5 set are 4-5 pages of bilevel
    (mode '1') CCITT-compressed scans at 1700×2200. The conversion to
    RGB is mandatory before JPEG encoding — saving a mode-'1' image as
    JPEG raises an OSError. The long-edge cap then trims the page so
    it stays within the Anthropic vision per-image budget.
    """

    pages: list[RenderedPage] = []
    with Image.open(path) as tiff:
        frame_count = getattr(tiff, "n_frames", 1)
        for index in range(frame_count):
            tiff.seek(index)
            # ``copy()`` detaches the frame from the seek cursor so the
            # subsequent iteration does not mutate the page we just
            # appended (a real Pillow gotcha on multi-frame formats).
            frame = tiff.copy().convert("RGB")
            frame = _cap_long_edge(frame)
            pages.append(
                RenderedPage(
                    page_number=index + 1,
                    image=frame,
                    width_px=frame.width,
                    height_px=frame.height,
                )
            )
    return pages


def _cap_long_edge(image: Image.Image) -> Image.Image:
    long_edge = max(image.width, image.height)
    if long_edge <= MAX_LONG_EDGE_PX:
        return image
    scale = MAX_LONG_EDGE_PX / long_edge
    new_size = (int(image.width * scale), int(image.height * scale))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def encode_jpeg_bytes(image: Image.Image, quality: int = 85) -> bytes:
    """Encode `image` as JPEG bytes for the Anthropic vision payload."""

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def encode_png_bytes(image: Image.Image) -> bytes:
    """Encode `image` as PNG bytes for the citation-overlay page cache.

    PNG (rather than JPEG) avoids the compression artifacts JPEG
    introduces around text glyphs and thin lines on lab printouts;
    those artifacts make bbox alignment hard to eyeball when the
    overlay UI flips a citation rectangle on hover. The size penalty
    is acceptable for the cache use case (one-time write per page,
    served on demand).
    """

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
