"""Disk-backed PNG cache for rendered document pages.

The bbox-overlay UI on the OpenEMR review pages needs to render the
source page underneath the citation rectangles. The OpenEMR docker
image (``openemr/openemr:flex``) ships ImageMagick but no Ghostscript,
so PHP cannot rasterize PDFs in-process; rendering happens here, where
``pypdfium2`` and Pillow are already installed for the VLM extractor.

Layout: ``<root>/<document_id>/page_<N>.png``. Module-level functions
mirror :mod:`clinical_copilot.documents.store` so the wiring shape is
the same — a ``root`` kwarg keeps tests off the shared default tree.

Cache miss is the route's responsibility to surface; this module
returns ``None`` from :func:`read` so the route can decide between a
404 with a structured body (current contract) or an on-demand re-render
(future, requires persisting source bytes — out of scope for the
overlay MVP).
"""

from __future__ import annotations

from pathlib import Path

# ``data/`` is gitignored at the repo level; the renders persist here
# alongside the existing ``data/extracted/`` JSON store.
_DEFAULT_CACHE_ROOT = Path(__file__).resolve().parents[3] / "data" / "page_renders"


def cache_root() -> Path:
    return _DEFAULT_CACHE_ROOT


def _document_dir(document_id: str, root: Path | None) -> Path:
    target_root = root or _DEFAULT_CACHE_ROOT
    return target_root / document_id


def _page_path(document_id: str, page_number: int, root: Path | None) -> Path:
    return _document_dir(document_id, root) / f"page_{page_number}.png"


def write(
    document_id: str,
    page_number: int,
    png_bytes: bytes,
    *,
    root: Path | None = None,
) -> Path:
    """Persist ``png_bytes`` for ``(document_id, page_number)``.

    Overwrites prior content for the same key. Page numbers are
    1-indexed to match :class:`clinical_copilot.documents.fetcher.RenderedPage`
    and the bbox citation's ``page`` field.
    """

    if page_number < 1:
        raise ValueError(f"page_number must be >= 1, got {page_number}")
    path = _page_path(document_id, page_number, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return path


def read(
    document_id: str,
    page_number: int,
    *,
    root: Path | None = None,
) -> bytes | None:
    """Return the cached PNG bytes, or ``None`` on cache miss."""

    path = _page_path(document_id, page_number, root)
    if not path.exists():
        return None
    return path.read_bytes()


def has(
    document_id: str,
    page_number: int,
    *,
    root: Path | None = None,
) -> bool:
    return _page_path(document_id, page_number, root).exists()


def page_count(document_id: str, *, root: Path | None = None) -> int:
    """Return the number of pages cached for ``document_id``.

    Zero when the document has never been rendered (or its directory is
    empty). Callers use this to distinguish "wrong page index" from
    "wrong document id" when surfacing 404 details.
    """

    directory = _document_dir(document_id, root)
    if not directory.is_dir():
        return 0
    return sum(1 for entry in directory.iterdir() if entry.suffix == ".png")
