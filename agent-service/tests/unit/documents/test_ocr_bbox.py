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


def _make_page(page_number: int, size: tuple[int, int] = (800, 1000)) -> RenderedPage:
    image = Image.new("RGB", size, color="white")
    return RenderedPage(
        page_number=page_number,
        image=image,
        width_px=size[0],
        height_px=size[1],
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
