"""Tests for SourceCitation + ExtractedField (PRD2 §6 + Appendix A.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def _valid_citation() -> SourceCitation:
    return SourceCitation(
        document_id="doc-1",
        page=1,
        bbox=(0.10, 0.20, 0.30, 0.25),
        confidence=0.92,
        raw_text="142",
    )


class TestSourceCitation:
    def test_valid_citation_round_trips(self) -> None:
        c = _valid_citation()
        assert c.page == 1
        assert c.bbox == (0.10, 0.20, 0.30, 0.25)

    def test_bbox_outside_unit_square_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceCitation(
                document_id="d",
                page=1,
                bbox=(0.0, 0.0, 1.5, 0.5),
                confidence=0.9,
                raw_text="x",
            )

    def test_degenerate_bbox_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceCitation(
                document_id="d",
                page=1,
                bbox=(0.5, 0.5, 0.5, 0.6),  # x1 == x0
                confidence=0.9,
                raw_text="x",
            )

    def test_page_must_be_one_indexed(self) -> None:
        with pytest.raises(ValidationError):
            SourceCitation(
                document_id="d",
                page=0,
                bbox=(0.0, 0.0, 0.1, 0.1),
                confidence=0.9,
                raw_text="x",
            )

    def test_confidence_outside_unit_interval_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceCitation(
                document_id="d",
                page=1,
                bbox=(0.0, 0.0, 0.1, 0.1),
                confidence=1.2,
                raw_text="x",
            )


class TestExtractedFieldXor:
    def test_value_with_citation_is_valid(self) -> None:
        f: ExtractedField[str] = ExtractedField(value="142 mg/dL", citation=_valid_citation())
        assert f.value == "142 mg/dL"
        assert f.abstain_reason is None

    def test_abstain_with_no_value_is_valid(self) -> None:
        f: ExtractedField[str] = ExtractedField(abstain_reason=RuntimeAbstainReason.NO_DATA)
        assert f.value is None
        assert f.abstain_reason is RuntimeAbstainReason.NO_DATA

    def test_value_without_citation_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedField[str](value="142")

    def test_value_and_abstain_together_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedField[str](
                value="142",
                citation=_valid_citation(),
                abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE,
            )

    def test_neither_value_nor_abstain_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedField[str]()

    def test_citation_without_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedField[str](
                citation=_valid_citation(),
                abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE,
            )
