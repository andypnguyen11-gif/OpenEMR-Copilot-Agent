"""Unit tests for OCR-based bbox tightening.

Most cases mock ``pytesseract.image_to_data`` so the matching logic is
exercised without dragging the real Tesseract binary into every test
run. One case (``test_ocr_page_recognises_real_text``) feeds a
PIL-rendered image through the real OCR call to confirm the
end-to-end wiring works on the developer machine; it's marked as a
soft skip so CI without Tesseract still passes.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from clinical_copilot.documents import ocr_bbox
from clinical_copilot.documents.fetcher import RenderedPage
from clinical_copilot.documents.ocr_bbox import (
    OcrWord,
    ocr_page,
    tighten_extracted_document_citations,
)


def _make_page(
    page_number: int,
    size: tuple[int, int] = (800, 1000),
    synthetic: bool = False,
) -> RenderedPage:
    image = Image.new("RGB", size, color="white")
    return RenderedPage(
        page_number=page_number,
        image=image,
        width_px=size[0],
        height_px=size[1],
        synthetic=synthetic,
    )


def _ocr_words(spec: list[tuple[str, tuple[float, float, float, float]]]) -> list[OcrWord]:
    return [OcrWord(text=t, bbox=b) for t, b in spec]


def _citation(
    *,
    page: int,
    bbox: list[float],
    raw_text: str,
    field_or_chunk_id: str = "field.path",
    document_id: str = "doc-1",
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "source_type": "extracted_document",
        "field_or_chunk_id": field_or_chunk_id,
        "document_id": document_id,
        "page": page,
        "bbox": bbox,
        "raw_text": raw_text,
        "confidence": confidence,
    }


def test_ocr_page_returns_empty_for_empty_image() -> None:
    image = Image.new("RGB", (1, 1), color="white")
    assert ocr_page(image) == []


def test_ocr_page_returns_empty_for_zero_dim_image() -> None:
    image = Image.new("RGB", (10, 10), color="white")
    image = image.resize((0, 0)) if False else image  # keep type happy
    # Stub: replicate the guard by passing a 0×0 image-shaped object.
    class _ZeroImage:
        width = 0
        height = 0

    assert ocr_page(_ZeroImage()) == []  # type: ignore[arg-type]


def test_tighten_replaces_bbox_when_raw_text_matches_ocr_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "patient_name": {
            "value": "Sofia Reyes",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.05, 0.05, 0.95, 0.55],  # coarse: top-half page chunk
                raw_text="Sofia Reyes",
            ),
        },
    }

    # OCR finds "Sofia" and "Reyes" inside the coarse hint — the
    # match should pin the citation bbox to the union of those words.
    fake_words = _ocr_words([
        ("Sofia", (0.10, 0.20, 0.18, 0.23)),
        ("Reyes", (0.19, 0.20, 0.27, 0.23)),
        ("Other", (0.10, 0.50, 0.20, 0.53)),  # decoy outside expected match
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])

    new_bbox = out["patient_name"]["citation"]["bbox"]
    # Tight rectangle: union of the two name words.
    assert new_bbox == [0.10, 0.20, 0.27, 0.23]
    # Original input was not mutated.
    assert facts["patient_name"]["citation"]["bbox"] == [0.05, 0.05, 0.95, 0.55]


def test_tighten_keeps_original_bbox_when_no_token_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "field": {
            "value": "Metformin 500mg",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.05, 0.05, 0.95, 0.55],
                raw_text="Metformin 500mg",
            ),
        },
    }
    fake_words = _ocr_words([
        ("Aspirin", (0.10, 0.20, 0.20, 0.23)),
        ("325mg", (0.21, 0.20, 0.30, 0.23)),
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])

    # Nothing matched → original coarse bbox preserved.
    assert out["field"]["citation"]["bbox"] == [0.05, 0.05, 0.95, 0.55]


def test_tighten_falls_back_to_full_page_when_hint_filters_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "field": {
            "value": "Sofia",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.0, 0.0, 0.05, 0.05],  # absurdly small/wrong hint
                raw_text="Sofia",
            ),
        },
    }
    # Real text lives outside the tiny hint — center filter would
    # exclude it. The fallback path searches the whole page.
    fake_words = _ocr_words([("Sofia", (0.40, 0.40, 0.48, 0.43))])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out["field"]["citation"]["bbox"] == [0.40, 0.40, 0.48, 0.43]


def test_tighten_uses_hint_to_disambiguate_repeated_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "field": {
            "value": "Sofia",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                # Hint: bottom half of the page.
                bbox=[0.0, 0.50, 1.0, 1.0],
                raw_text="Sofia",
            ),
        },
    }
    # "Sofia" appears at top AND bottom. Only the bottom one is inside
    # the hint, so that's the one we should pin to.
    fake_words = _ocr_words([
        ("Sofia", (0.10, 0.10, 0.18, 0.13)),  # top — outside hint
        ("Sofia", (0.10, 0.70, 0.18, 0.73)),  # bottom — inside hint
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out["field"]["citation"]["bbox"] == [0.10, 0.70, 0.18, 0.73]


def test_tighten_skips_non_extracted_document_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "field_with_guideline": {
            "value": "Aspirin",
            "abstain_reason": None,
            "citation": {
                "source_type": "guideline",
                "field_or_chunk_id": "g1#2",
                "chunk_id": "g1#2",
                "source_url": "https://example.test/g",
            },
        },
    }
    monkeypatch.setattr(
        ocr_bbox,
        "ocr_page",
        lambda _img: pytest.fail("ocr_page must not be called for non-extracted citations"),
    )
    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out == facts


def test_tighten_skips_citations_on_unknown_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = {
        "field": {
            "value": "x",
            "abstain_reason": None,
            "citation": _citation(
                page=99,  # no rendered_pages entry for page 99
                bbox=[0.0, 0.0, 1.0, 1.0],
                raw_text="x",
            ),
        },
    }
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: pytest.fail("should not OCR"))

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out["field"]["citation"]["bbox"] == [0.0, 0.0, 1.0, 1.0]


def test_tighten_returns_input_when_no_pages_provided() -> None:
    facts = {
        "field": {
            "value": "x",
            "abstain_reason": None,
            "citation": _citation(page=1, bbox=[0.0, 0.0, 1.0, 1.0], raw_text="x"),
        },
    }
    assert tighten_extracted_document_citations(facts, []) == facts


def test_tighten_walks_nested_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = {
        "items": [
            {
                "value": "Foo",
                "abstain_reason": None,
                "citation": _citation(
                    page=1, bbox=[0.0, 0.0, 1.0, 1.0], raw_text="Foo",
                    field_or_chunk_id="items[0].value",
                ),
            },
            {
                "value": "Bar",
                "abstain_reason": None,
                "citation": _citation(
                    page=1, bbox=[0.0, 0.0, 1.0, 1.0], raw_text="Bar",
                    field_or_chunk_id="items[1].value",
                ),
            },
        ],
    }
    fake_words = _ocr_words([
        ("Foo", (0.10, 0.10, 0.18, 0.13)),
        ("Bar", (0.50, 0.80, 0.58, 0.83)),
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out["items"][0]["citation"]["bbox"] == [0.10, 0.10, 0.18, 0.13]
    assert out["items"][1]["citation"]["bbox"] == [0.50, 0.80, 0.58, 0.83]


def test_tighten_caches_ocr_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many citations share a page (typical after one coarse bbox is
    reused across fields). OCR runs once per page, not per citation.
    """

    facts = {
        f"field_{i}": {
            "value": "x",
            "abstain_reason": None,
            "citation": _citation(
                page=1, bbox=[0.0, 0.0, 1.0, 1.0], raw_text="x",
                field_or_chunk_id=f"field_{i}",
            ),
        }
        for i in range(5)
    }
    call_count = {"n": 0}

    def fake_ocr(_img: Any) -> list[OcrWord]:
        call_count["n"] += 1
        return _ocr_words([("x", (0.10, 0.10, 0.12, 0.13))])

    monkeypatch.setattr(ocr_bbox, "ocr_page", fake_ocr)
    tighten_extracted_document_citations(facts, [_make_page(1)])

    assert call_count["n"] == 1, "OCR should be cached per page"


def test_tighten_rejects_low_coverage_match_and_keeps_original_bbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OCR finds only a small fraction of the target tokens, the
    tightened bbox would be precise-but-wrong (single word in the
    wrong row). Better to keep the original coarse bbox so the UI
    visually communicates "approximate" rather than mislead the
    clinician with a tight rectangle on the wrong text.
    """

    facts = {
        "field": {
            "value": "Mother: None reported, deceased age 81 (natural)",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.04, 0.78, 0.96, 0.802],
                raw_text="Mother | None reported | — | Deceased age 81 (natural)",
            ),
        },
    }
    # Only "None" matches out of {mother, none, reported, deceased,
    # age, 81, natural} — coverage = 1/7 ≈ 0.14, below the 0.5 floor.
    fake_words = _ocr_words([
        ("None", (0.10, 0.10, 0.18, 0.13)),
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])

    # Tight match was rejected → original coarse bbox preserved.
    assert out["field"]["citation"]["bbox"] == [0.04, 0.78, 0.96, 0.802]


def test_tighten_prefers_full_page_match_when_hint_misses_most_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the AI's hint bbox excludes the page region where the
    citation actually lives and the OCR match is contained within a
    region smaller than the hint, the full-page pass should win.
    """

    facts = {
        "field": {
            "value": "Stroke",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                # Wrong hint: top-left of page. The real text is at
                # bottom-right but its bbox is still smaller than the
                # hint, so the area-shrink check passes.
                bbox=[0.05, 0.05, 0.95, 0.30],
                raw_text="Father Stroke CVA Deceased",
            ),
        },
    }
    fake_words = _ocr_words([
        # Top-of-page words inside the hint — none match target tokens.
        ("Header", (0.10, 0.10, 0.20, 0.13)),
        # Bottom-of-page words outside the hint — all 4 target tokens
        # match, coverage 4/4 = 1.00 (full-page pass wins).
        ("Father", (0.10, 0.78, 0.18, 0.81)),
        ("Stroke", (0.20, 0.78, 0.28, 0.81)),
        ("CVA", (0.30, 0.78, 0.36, 0.81)),
        ("Deceased", (0.50, 0.78, 0.62, 0.81)),
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    new_bbox = out["field"]["citation"]["bbox"]
    # Should land at the bottom row (full-page winner), not at the
    # top hint region.
    assert new_bbox[1] >= 0.7, f"expected bottom-of-page bbox, got {new_bbox}"


def test_tighten_rejects_match_that_balloons_beyond_hint_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When greedy matching grabs OCR words from multiple page regions
    (e.g. the same token appears twice), the union bbox can end up
    larger than the AI's hint. Reject those — keeping the coarse hint
    is better than a precise-but-wrong rectangle covering most of the
    page.
    """

    facts = {
        "field": {
            "value": "Stroke",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                # Small hint near the top.
                bbox=[0.05, 0.05, 0.30, 0.15],
                raw_text="Father Stroke 70",
            ),
        },
    }
    fake_words = _ocr_words([
        # Greedy first-match would grab these scattered words — coverage
        # 3/3 but bbox spans top-to-bottom (area > hint area). Reject.
        ("Father", (0.06, 0.10, 0.10, 0.13)),  # inside hint
        ("Stroke", (0.40, 0.50, 0.50, 0.55)),  # mid-page
        ("70", (0.20, 0.85, 0.25, 0.88)),       # bottom
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    # Match was rejected (area too big) → original coarse bbox preserved.
    assert out["field"]["citation"]["bbox"] == [0.05, 0.05, 0.30, 0.15]


def test_tighten_breaks_ties_by_distance_to_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hint and full-page passes tie on coverage, prefer the
    one closer to the AI's hint center — the AI's region guess is a
    better-than-nothing prior even if it's wrong by a few percent.
    """

    facts = {
        "field": {
            "value": "Sofia",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.0, 0.45, 1.0, 0.55],
                raw_text="Sofia",
            ),
        },
    }
    # Two "Sofia" occurrences with full coverage: one inside the hint,
    # one near the top. Hint should win because it's closer.
    fake_words = _ocr_words([
        ("Sofia", (0.10, 0.10, 0.18, 0.13)),  # outside hint, top
        ("Sofia", (0.40, 0.48, 0.48, 0.51)),  # inside hint, middle
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    assert out["field"]["citation"]["bbox"] == [0.40, 0.48, 0.48, 0.51]


def test_tighten_does_not_pull_tokens_across_adjacent_table_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the Apixaban row on the Whitaker intake had a
    low-confidence "5 mg" cell that Tesseract dropped. The matcher
    then reached into the *next* table row to satisfy the "mg" target
    token — and the resulting Apixaban bbox extended down into the
    Tamsulosin row, visually overlapping it.

    Adjacent rows in a tabular layout are ~0.025 of page height
    apart; the anchor-distance cap should reject cross-row pulls of
    that magnitude even when a target token is missing from the
    anchor's own row.
    """

    facts = {
        "field": {
            "value": "Apixaban 5 mg PO daily",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.04, 0.59, 0.96, 0.62],
                raw_text="Apixaban 5 mg PO daily",
            ),
        },
    }
    # Simulate the real Whitaker layout: Apixaban row at y≈0.63,
    # Tamsulosin row at y≈0.66 (centers ~0.025 apart). The "5 mg"
    # cell is missing from the Apixaban row (low Tesseract conf,
    # dropped at the OCR threshold). Only the Tamsulosin row has
    # a "mg" token. Pulling it would extend the bbox into y≥0.66.
    fake_words = _ocr_words([
        ("Apixaban", (0.07, 0.630, 0.13, 0.642)),
        ("PO", (0.33, 0.630, 0.35, 0.640)),
        ("daily", (0.40, 0.630, 0.43, 0.642)),
        ("Tamsulosin", (0.07, 0.654, 0.15, 0.669)),
        ("mg", (0.27, 0.658, 0.29, 0.667)),
        ("PO", (0.33, 0.656, 0.35, 0.665)),
        ("daily", (0.36, 0.656, 0.39, 0.667)),
    ])
    monkeypatch.setattr(ocr_bbox, "ocr_page", lambda _img: fake_words)

    out = tighten_extracted_document_citations(facts, [_make_page(1)])
    new_bbox = out["field"]["citation"]["bbox"]
    # bbox must end before the Tamsulosin row begins (y < 0.654);
    # a value of 0.667 (Tamsulosin "mg" bottom) means we pulled
    # across rows.
    assert new_bbox[3] < 0.65, (
        f"bbox bottom {new_bbox[3]:.4f} should stay above the next row at 0.654"
    )


def test_tighten_skips_synthetic_pages_and_preserves_original_bbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: HL7 / docx / xlsx pages produced by the synthetic
    text-on-page renderer carry exact, deterministic bboxes from the
    extractor. Running OCR-tightening on them bleeds bboxes across
    adjacent rows because the line spacing (~0.015 of page height)
    is tighter than the cross-row anchor cap (0.022) and tokens like
    ``OBX`` / ``Cholesterol`` / ``LN`` repeat across HL7 segments.
    The synthetic flag must short-circuit OCR-tightening so the
    extractor's bbox passes through unchanged.
    """

    facts = {
        "obs_a": {
            "value": "first row",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.034, 0.115, 0.962, 0.130],
                raw_text="OBX|2|NM|2089-1^Cholesterol in LDL [Mass/volume]^LN||140",
            ),
        },
        "obs_b": {
            "value": "next row",
            "abstain_reason": None,
            "citation": _citation(
                page=1,
                bbox=[0.034, 0.130, 0.962, 0.145],
                raw_text="OBX|3|NM|2085-9^Cholesterol in HDL [Mass/volume]^LN||48",
            ),
        },
    }
    # If OCR were called, this would pull tokens across rows and
    # collapse both bboxes onto one. The fail() here proves the
    # synthetic short-circuit is wired.
    monkeypatch.setattr(
        ocr_bbox,
        "ocr_page",
        lambda _img: pytest.fail("ocr_page must not run on synthetic pages"),
    )

    out = tighten_extracted_document_citations(
        facts, [_make_page(1, synthetic=True)],
    )
    assert out["obs_a"]["citation"]["bbox"] == [0.034, 0.115, 0.962, 0.130]
    assert out["obs_b"]["citation"]["bbox"] == [0.034, 0.130, 0.962, 0.145]


@pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Tesseract binary not installed; CI / minimal envs skip the real-OCR smoke",
)
def test_ocr_page_recognises_real_text() -> None:
    """End-to-end smoke: render a known string into a PIL image,
    OCR it, confirm at least one of the rendered tokens comes back.

    The exact bbox depends on the rendered font and Tesseract version,
    so we assert on token presence and on the bbox lying inside the
    expected half of the image, not on pixel-perfect coordinates.
    """

    image = Image.new("RGB", (800, 200), color="white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
    except OSError:
        font = ImageFont.load_default()
    # Render in the LEFT half of the image so we can assert position.
    draw.text((40, 60), "Sofia Reyes", fill="black", font=font)

    words = ocr_page(image)
    word_texts = {w.text.lower() for w in words}
    assert "sofia" in word_texts or "reyes" in word_texts, (
        f"Expected to find Sofia/Reyes in OCR output, got {sorted(word_texts)}"
    )

    matches = [w for w in words if w.text.lower() in {"sofia", "reyes"}]
    for w in matches:
        # All matches should sit in the left half of the image.
        cx = (w.bbox[0] + w.bbox[2]) / 2
        assert cx < 0.55, f"OCR placed {w.text!r} at center x={cx} (expected < 0.55)"
