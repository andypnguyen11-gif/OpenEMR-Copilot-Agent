"""OCR-based bounding-box tightening for extracted-document citations.

The Anthropic vision extractor returns one coarse ``bbox`` per
page-chunk and reuses it across every field on that chunk — which
makes the bbox-overlay UI's click-to-highlight functionally useless
because every row points at the same rectangle. This module
post-processes the extractor output by running Tesseract on the
already-rendered page images, finding each citation's ``raw_text`` in
the OCR output, and replacing the coarse bbox with the union bbox of
the matched OCR words.

OCR is best-effort: if the match fails (raw_text not on page, OCR
gibberish from a low-quality fax, no overlap with the hint bbox), the
citation keeps its original coarse bbox. The overlay still draws
something — it's just less precise — instead of dropping the citation
entirely.

The hint bbox (the AI's coarse rectangle) is used as a *region
filter*, not a quality gate: we restrict OCR-word matches to words
whose center lies inside the hint, since the AI's bbox is wrong by
being too big rather than too small. That filter avoids cross-region
false matches (the same word appearing twice on the same page).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytesseract
from PIL import Image

from clinical_copilot.documents.fetcher import RenderedPage


@dataclass(frozen=True, slots=True)
class OcrWord:
    """Single Tesseract-recognised word + its bbox normalized to [0,1].

    Storing normalized coordinates (rather than image pixels) keeps the
    OcrWord shape compatible with :class:`SourceCitation`'s
    bbox contract — both sides of the matching live in the same
    coordinate space.
    """

    text: str
    bbox: tuple[float, float, float, float]


# Confidence threshold below which a Tesseract match is too noisy to
# be useful for bbox tightening. Tesseract emits -1 for non-words
# (whitespace tokens) and 0..100 for actual recognitions; on a clean
# scan most real words land in the 60-95 range.
_MIN_CONFIDENCE: int = 30


def ocr_page(image: Image.Image) -> list[OcrWord]:
    """Run Tesseract on ``image`` and return one ``OcrWord`` per
    recognised word with normalized bbox coordinates.

    Empty / whitespace-only entries and tokens below
    :data:`_MIN_CONFIDENCE` are dropped. The order matches Tesseract's
    reading-order output (top-to-bottom, left-to-right within a line).
    """

    if image.width <= 0 or image.height <= 0:
        return []

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    width = float(image.width)
    height = float(image.height)
    words: list[OcrWord] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if text == "":
            continue
        try:
            confidence = int(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if confidence < _MIN_CONFIDENCE:
            continue
        left = float(data["left"][i]) / width
        top = float(data["top"][i]) / height
        right = (float(data["left"][i]) + float(data["width"][i])) / width
        bottom = (float(data["top"][i]) + float(data["height"][i])) / height
        # Clamp to [0,1] in case Tesseract returns coordinates that
        # exceed image dims by a pixel or two on the edges.
        words.append(
            OcrWord(
                text=text,
                bbox=(
                    max(0.0, min(1.0, left)),
                    max(0.0, min(1.0, top)),
                    max(0.0, min(1.0, right)),
                    max(0.0, min(1.0, bottom)),
                ),
            )
        )
    return words


def tighten_extracted_document_citations(
    facts_dict: dict[str, Any],
    rendered_pages: list[RenderedPage],
) -> dict[str, Any]:
    """Return ``facts_dict`` with every extracted-document citation's
    ``bbox`` replaced by an OCR-derived tight bbox where possible.

    The input dict is not mutated; a new tree is returned so the
    caller can compare the before/after JSON. Per-page OCR runs once
    even when many citations share the same page (the typical case
    after the extractor reuses one coarse bbox across fields).

    Citations on pages outside ``rendered_pages`` (e.g. the page count
    diverged from the renderer) keep their original bbox. Same for
    citations whose ``raw_text`` doesn't match anything in the OCR
    output for that page.
    """

    if not rendered_pages:
        return facts_dict

    # Cache: page_number → ocr_words. Lazy so a facts tree that doesn't
    # touch a page never pays for OCR'ing it.
    pages_by_number = {p.page_number: p for p in rendered_pages}
    ocr_cache: dict[int, list[OcrWord]] = {}

    def get_ocr(page_number: int) -> list[OcrWord] | None:
        if page_number in ocr_cache:
            return ocr_cache[page_number]
        page = pages_by_number.get(page_number)
        if page is None:
            return None
        ocr_cache[page_number] = ocr_page(page.image)
        return ocr_cache[page_number]

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            new_node = {}
            for key, value in node.items():
                if (
                    key == "citation"
                    and isinstance(value, dict)
                    and value.get("source_type") == "extracted_document"
                ):
                    new_node[key] = _maybe_tighten_citation(value, get_ocr)
                else:
                    new_node[key] = walk(value)
            return new_node
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(facts_dict)


def _maybe_tighten_citation(
    citation: dict[str, Any],
    get_ocr: Any,
) -> dict[str, Any]:
    page_num = citation.get("page")
    raw_text = citation.get("raw_text")
    bbox = citation.get("bbox")
    if not isinstance(page_num, int) or page_num < 1:
        return citation
    if not isinstance(raw_text, str) or raw_text == "":
        return citation
    if not isinstance(bbox, list) or len(bbox) != 4:
        return citation

    ocr_words = get_ocr(page_num)
    if not ocr_words:
        return citation

    hint_bbox = (
        float(bbox[0]),
        float(bbox[1]),
        float(bbox[2]),
        float(bbox[3]),
    )
    tightened = _find_tight_bbox(raw_text, ocr_words, hint_bbox)
    if tightened is None:
        return citation
    return {**citation, "bbox": list(tightened)}


def _find_tight_bbox(
    raw_text: str,
    ocr_words: list[OcrWord],
    hint_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """Tokenize ``raw_text``, match tokens against ``ocr_words`` whose
    center lies inside ``hint_bbox``, and return the union bbox of
    matched words. Returns ``None`` when no token finds a home.
    """

    target_tokens = _tokenize(raw_text)
    if not target_tokens:
        return None

    candidates = [w for w in ocr_words if _center_inside(w.bbox, hint_bbox)]
    if not candidates:
        # AI's hint was so coarse it didn't even contain itself, or the
        # OCR text drifted outside it — fall back to the full page so
        # we still get a tighter rectangle than the page-chunk hint.
        candidates = ocr_words

    matched: list[OcrWord] = []
    used_indices: set[int] = set()
    for token in target_tokens:
        # First-match wins, but skip OCR words we've already consumed
        # so a citation like "Sofia Reyes Sofia Reyes" doesn't collapse
        # both target tokens onto the first OCR "Sofia".
        for idx, word in enumerate(candidates):
            if idx in used_indices:
                continue
            if token in _word_tokens(word):
                matched.append(word)
                used_indices.add(idx)
                break

    if not matched:
        return None

    x0 = min(w.bbox[0] for w in matched)
    y0 = min(w.bbox[1] for w in matched)
    x1 = max(w.bbox[2] for w in matched)
    y1 = max(w.bbox[3] for w in matched)
    return (x0, y0, x1, y1)


_TOKEN_SPLIT = re.compile(r"\W+", flags=re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-word characters, drop empties.

    Punctuation and whitespace are equivalent boundaries. The OCR
    side does the same so a citation `raw_text` of ``"DOB: 07/04/1983"``
    matches OCR words ``DOB``, ``07``, ``04``, ``1983`` independently.
    """

    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def _word_tokens(word: OcrWord) -> set[str]:
    return set(_tokenize(word.text))


def _center_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> bool:
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]
