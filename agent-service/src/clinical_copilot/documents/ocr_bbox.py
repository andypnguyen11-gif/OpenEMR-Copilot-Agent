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

# Token-coverage floor below which a tightened bbox is rejected back
# to the original coarse bbox. Without this guard, matching just one
# token out of nine (e.g. only "None" survived from a citation like
# "Mother: None reported, deceased age 81") collapses the rectangle
# to a single-word position that may sit on the wrong row entirely
# — worse than the original coarse box, because the precision
# implies the location is correct. The 0.5 floor keeps half-or-better
# matches and discards low-coverage ones.
_MIN_TOKEN_COVERAGE: float = 0.5

# Absolute area cap on the matched bbox (as a fraction of page area).
# Matches whose union bbox spans more than this are likely the result
# of greedy first-match grabbing OCR words from multiple page regions
# — the citation can't physically cover a third of the page, so any
# match that big is a sign the matching went wrong. Reject and keep
# the original coarse bbox.
_MAX_MATCH_AREA: float = 0.30

# Maximum vertical distance (as a fraction of page height) between
# the cluster anchor and any subsequent OCR word added to the match.
# Sized so that adjacent table rows (typically ~0.025 apart on the
# Whitaker intake) cannot pull tokens into each other, while a value
# wrapped across two lines of the same paragraph (line spacing
# ~0.018-0.020) still clusters together.
#
# Was 0.05 originally; tightened after observing Apixaban's bbox
# bleeding into the Tamsulosin row when Tesseract dropped the
# low-confidence "5 mg" cell — the matcher reached two rows down
# for the next "mg" token, since the 0.05 cap admitted it.
_MAX_ANCHOR_Y_DISTANCE: float = 0.022


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
    """Tokenize ``raw_text`` and return the best OCR-derived bbox, or
    ``None`` if no candidate clears :data:`_MIN_TOKEN_COVERAGE`.

    Two passes: one restricted to OCR words whose center sits inside
    the AI's coarse ``hint_bbox`` (handles the "same word appears
    twice on the page" case by using the AI's region as a hint), and
    one over every OCR word on the page (rescues citations whose hint
    was wrong by a few percent). The pass with higher token coverage
    wins; ties break by distance to the hint center so the closer
    rectangle wins.

    Low-coverage matches are dropped entirely. Returning ``None``
    keeps the original coarse bbox in the citation — better to draw
    an approximate rectangle than a precise wrong one, because the
    UI gives the same visual weight to both.
    """

    target_tokens = _tokenize(raw_text)
    if not target_tokens:
        return None

    hint_candidates = [w for w in ocr_words if _center_inside(w.bbox, hint_bbox)]
    hint_match = (
        _match_tokens(target_tokens, hint_candidates, hint_bbox=hint_bbox)
        if hint_candidates
        else None
    )
    full_match = _match_tokens(target_tokens, ocr_words, hint_bbox=hint_bbox)

    def is_acceptable(m: _MatchResult | None) -> bool:
        if m is None:
            return False
        if m.coverage < _MIN_TOKEN_COVERAGE:
            return False
        # Absolute area cap (not relative to hint). Some AI hints are
        # off by more than 10% of page height (observed against a
        # Whitaker intake where the hint pointed at ALLERGIES but the
        # cited Father row was actually 12% lower). Restricting the
        # match to be smaller than the hint would reject the correct
        # full-page match in those cases. The fixed cap still catches
        # the genuine "tokens scattered across the page" failure mode.
        match_area = (m.bbox[2] - m.bbox[0]) * (m.bbox[3] - m.bbox[1])
        if match_area > _MAX_MATCH_AREA:
            return False
        return True

    accepted: list[_MatchResult] = [m for m in (hint_match, full_match) if is_acceptable(m)]
    if not accepted:
        return None

    hint_center = (
        (hint_bbox[0] + hint_bbox[2]) / 2.0,
        (hint_bbox[1] + hint_bbox[3]) / 2.0,
    )

    def score(match: _MatchResult) -> tuple[float, float]:
        # Higher coverage wins; tiebreak by closer-to-hint (negate
        # distance so smaller distance ranks higher under ``max``).
        bbox_center = (
            (match.bbox[0] + match.bbox[2]) / 2.0,
            (match.bbox[1] + match.bbox[3]) / 2.0,
        )
        dx = bbox_center[0] - hint_center[0]
        dy = bbox_center[1] - hint_center[1]
        distance = (dx * dx + dy * dy) ** 0.5
        return (match.coverage, -distance)

    return max(accepted, key=score).bbox


@dataclass(frozen=True, slots=True)
class _MatchResult:
    bbox: tuple[float, float, float, float]
    matched_count: int
    coverage: float


def _match_tokens(
    target_tokens: list[str],
    candidates: list[OcrWord],
    hint_bbox: tuple[float, float, float, float] | None = None,
) -> _MatchResult | None:
    """Cluster-expansion token alignment.

    Pure greedy first-match-wins fails on repeated tokens: when a
    citation's raw_text contains a generic word like ``Active``,
    ``PO``, or ``mg`` that appears in many rows of a table, the
    matcher picks the first occurrence and the union bbox stretches
    to span unrelated rows.

    Instead, for every OCR word that matches *some* target token, try
    using it as a row-anchor. Expand by matching the remaining target
    tokens to the OCR word *closest* (by vertical distance) to the
    anchor — that keeps repeated-token matches inside one row. Score
    candidates by (coverage, closer-to-hint, smaller bbox area) so a
    cluster that lands near the AI's region guess wins ties over a
    cluster of the same size in a different row.

    The hint-proximity tiebreak matters when two clusters cover the
    same number of target tokens but sit in different rows — common
    when the AI's hint is off by ~1 row and a generic token like
    ``mg`` appears in both the citation's actual row and the next.
    Without it, the area-only tiebreak silently prefers whichever
    cluster happens to be tighter, which is a coin flip relative to
    correctness.
    """

    if not target_tokens or not candidates:
        return None

    target_token_set = set(target_tokens)

    def expand(seed_idx: int) -> _MatchResult | None:
        used: set[int] = {seed_idx}
        anchor = candidates[seed_idx]
        anchor_y = (anchor.bbox[1] + anchor.bbox[3]) / 2.0
        matched: list[OcrWord] = [anchor]

        # Track which distinct target tokens have been satisfied by
        # the matched OCR words. This is keyed off OCR word *content*
        # rather than per-target-position so that a single OCR word
        # like ``N40.0`` (which tokenizes to {n40, 0}) credits both
        # target tokens "n40" and "0" without forcing target "0" to
        # find a *different* OCR word — which is what was pulling
        # bboxes across rows on the Whitaker problem-list table.
        satisfied_tokens: set[str] = _word_tokens(anchor) & target_token_set

        for token in target_tokens:
            if token in satisfied_tokens:
                continue
            best_idx: int | None = None
            best_distance = float("inf")
            for idx, word in enumerate(candidates):
                if idx in used:
                    continue
                if token not in _word_tokens(word):
                    continue
                # Pull from the row closest to the anchor in y.
                w_y = (word.bbox[1] + word.bbox[3]) / 2.0
                distance = abs(w_y - anchor_y)
                if distance > _MAX_ANCHOR_Y_DISTANCE:
                    continue
                if distance < best_distance:
                    best_distance = distance
                    best_idx = idx
            if best_idx is not None:
                matched.append(candidates[best_idx])
                used.add(best_idx)
                satisfied_tokens |= _word_tokens(candidates[best_idx]) & target_token_set

        if not matched:
            return None
        x0 = min(w.bbox[0] for w in matched)
        y0 = min(w.bbox[1] for w in matched)
        x1 = max(w.bbox[2] for w in matched)
        y1 = max(w.bbox[3] for w in matched)
        # Coverage is the fraction of *distinct* target tokens that
        # any matched OCR word's text covered. For target ``[n40, 0]``
        # matched by a single ``N40.0`` OCR word, coverage is 2/2,
        # not 1/2 — we did satisfy both target tokens.
        unique_target_tokens = target_token_set
        coverage = (
            len(satisfied_tokens & unique_target_tokens) / len(unique_target_tokens)
            if unique_target_tokens
            else 0.0
        )
        return _MatchResult(
            bbox=(x0, y0, x1, y1),
            matched_count=len(matched),
            coverage=coverage,
        )

    hint_center = (
        ((hint_bbox[0] + hint_bbox[2]) / 2.0, (hint_bbox[1] + hint_bbox[3]) / 2.0)
        if hint_bbox is not None
        else None
    )

    best: _MatchResult | None = None
    best_score: tuple[float, float, float] | None = None
    for seed_idx, seed in enumerate(candidates):
        # Only seed on OCR words that match at least one target token —
        # other words can't anchor a useful cluster.
        if not (_word_tokens(seed) & target_token_set):
            continue
        attempt = expand(seed_idx)
        if attempt is None:
            continue
        area = (attempt.bbox[2] - attempt.bbox[0]) * (attempt.bbox[3] - attempt.bbox[1])
        if hint_center is not None:
            # Y-only distance. The AI hint is essentially a row
            # indicator — it almost always spans the full text width
            # of the page, so its x-center carries no information
            # about which column the citation lives in. Including x
            # in the distance lets a narrow, off-row cluster beat a
            # wide, on-row cluster.
            cluster_y = (attempt.bbox[1] + attempt.bbox[3]) / 2.0
            hint_distance = abs(cluster_y - hint_center[1])
        else:
            hint_distance = 0.0
        # Prefer higher coverage, then closer to the AI hint, then
        # tighter bbox. Hint-proximity beats area because two clusters
        # of the same coverage in different rows are nearly always
        # disambiguated by the hint, while area is unrelated to row
        # correctness.
        score = (attempt.coverage, -hint_distance, -area)
        if best_score is None or score > best_score:
            best = attempt
            best_score = score

    return best


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
