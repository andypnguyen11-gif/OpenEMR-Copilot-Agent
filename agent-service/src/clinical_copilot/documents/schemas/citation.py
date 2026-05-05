"""SourceCitation + ExtractedField (PRD2 §6, Appendix A.1).

A ``SourceCitation`` is the formal handle the verification layer uses to
re-resolve a field back to the document region the VLM claimed it came
from. An ``ExtractedField[T]`` couples a value with its citation, or
with an abstention reason if no value could be produced.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinical_copilot.schemas.abstain import RuntimeAbstainReason

T = TypeVar("T")


class SourceCitation(BaseModel):
    """Pointer back to the document region a fact was extracted from.

    ``bbox`` is normalized to the page (0..1 in both axes) so the same
    citation resolves correctly whether the page is rendered at 150 DPI
    for a thumbnail or 300 DPI for the OCR check.

    ``confidence`` is the VLM's self-reported per-field confidence; the
    extractor down-converts a value to ``LOW_CONFIDENCE`` when this is
    below the §6 threshold (0.7).

    ``raw_text`` is the verbatim string the VLM claims sits inside
    ``bbox``. The OCR check (PRD2 §8.2) compares it against what
    Tesseract reads from the rendered region.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)
    raw_text: str

    @model_validator(mode="after")
    def _bbox_in_unit_square(self) -> "SourceCitation":
        x0, y0, x1, y1 = self.bbox
        for v in self.bbox:
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"bbox component out of [0,1]: {self.bbox}")
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"bbox is degenerate (x1<=x0 or y1<=y0): {self.bbox}")
        return self


class ExtractedField(BaseModel, Generic[T]):
    """Generic field carrier — either a (value, citation) pair or an
    abstention reason. The ``value_xor_abstain`` validator enforces the
    exclusive-or; downstream code can rely on this without re-checking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T | None = None
    citation: SourceCitation | None = None
    abstain_reason: RuntimeAbstainReason | None = None

    @model_validator(mode="after")
    def value_xor_abstain(self) -> "ExtractedField[T]":
        has_value = self.value is not None
        has_citation = self.citation is not None
        has_abstain = self.abstain_reason is not None

        if has_value and has_abstain:
            raise ValueError(
                "ExtractedField cannot carry both a value and an abstain_reason"
            )
        if has_value and not has_citation:
            raise ValueError(
                "ExtractedField with a value must also carry a citation"
            )
        if not has_value and not has_abstain:
            raise ValueError(
                "ExtractedField with no value must carry an abstain_reason"
            )
        if not has_value and has_citation:
            raise ValueError(
                "ExtractedField without a value must not carry a citation"
            )
        return self
