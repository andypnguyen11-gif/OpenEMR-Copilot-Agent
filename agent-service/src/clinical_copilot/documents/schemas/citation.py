"""SourceCitation + ExtractedField (PRD2 §6, Appendix A.1).

A ``SourceCitation`` is the formal handle the verification layer uses to
re-resolve a field back to the document region the VLM claimed it came
from. An ``ExtractedField[T]`` couples a value with its citation, or
with an abstention reason if no value could be produced.

Three citation types share a discriminator on ``source_type``:
``SourceCitation`` (extracted documents — page+bbox), ``GuidelineCitation``
(retrieval chunks — chunk_id+source_url), ``PatientChartCitation`` (FHIR
resources — resource_type+resource_id). The ``Citation`` union below is the
type used wherever a wire-shape carries a citation regardless of origin.

Discriminator-value style: bare ``Literal["..."]`` rather than
``Literal[CitationSourceType.X]``. The StrEnum is exported for callers but
not embedded in the type-system discriminator — Pydantic v2 has known
edge cases resolving enum-valued discriminators across union members
defined in the same module, and the bare-string form is functionally
identical at the JSON wire level. ``CitationSourceType`` stays the
canonical name set so downstream code can use ``CitationSourceType.GUIDELINE``
without hardcoding ``"guideline"``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinical_copilot.schemas.abstain import RuntimeAbstainReason

T = TypeVar("T")


class CitationSourceType(StrEnum):
    """Canonical names for the three citation discriminators.

    The string values are the wire form. Use the enum members in code
    (``CitationSourceType.GUIDELINE``); the class ``Literal[...]``
    discriminators on the citation models accept the same string values.
    """

    EXTRACTED_DOCUMENT = "extracted_document"
    GUIDELINE = "guideline"
    PATIENT_CHART = "patient_chart"


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

    ``field_or_chunk_id`` is a JSON-pointer-style path identifying the
    extracted-record leaf this citation belongs to (e.g.
    ``observations[0].value``, ``medications[2].dose``). For extracted
    documents this is the schema-walk path, set by the extractor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: Literal["extracted_document"] = "extracted_document"
    document_id: str
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)
    raw_text: str
    field_or_chunk_id: str = Field(
        default="",
        description=(
            "TRANSITIONAL DEFAULT (PR 1a -> 1b): empty string while extractor "
            "constructor sites still build SourceCitation without threading the "
            "schema-walk path. PR 1b adds build_extracted_citation(path, ...) "
            "across extractor.py + 7 adapters and drops this default. Do NOT "
            "rely on the empty-string fallback in new code."
        ),
    )

    @model_validator(mode="after")
    def _bbox_in_unit_square(self) -> "SourceCitation":
        x0, y0, x1, y1 = self.bbox
        for v in self.bbox:
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"bbox component out of [0,1]: {self.bbox}")
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"bbox is degenerate (x1<=x0 or y1<=y0): {self.bbox}")
        return self


class GuidelineCitation(BaseModel):
    """Pointer to a retrieval chunk from the guideline corpus.

    ``chunk_id`` is the corpus-internal id of the indexed chunk;
    ``source_doc_id`` is the parent document id. ``source_url`` is the
    public URL when the corpus has one (NIH guideline PDFs etc.); None for
    private/unsourced material.

    ``field_or_chunk_id`` mirrors ``chunk_id`` to satisfy the discriminated-
    union contract — the wire-shape carries one canonical id field per
    citation type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: Literal["guideline"] = "guideline"
    field_or_chunk_id: str
    source_doc_id: str
    chunk_id: str
    source_url: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    raw_text: str


class PatientChartCitation(BaseModel):
    """Pointer to a FHIR resource in the patient's chart pack.

    ``resource_type`` and ``resource_id`` together address the resource
    (e.g. ``Observation/123``). ``field_or_chunk_id`` mirrors
    ``f"{resource_type}/{resource_id}"`` for the discriminated-union contract.

    ``display_summary`` is a ONE-LINE LABEL (e.g. ``"Glucose 142 mg/dL on
    2026-04-15"``) — NEVER the verbatim FHIR resource text. The full resource
    is re-fetched on demand via ``resource_type/resource_id``. Storing the
    verbatim resource here would create a PHI redaction surface and duplicate
    data the UI does not render. Producers building this type must pipe a
    short, human-readable summary; never raw resource JSON.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: Literal["patient_chart"] = "patient_chart"
    field_or_chunk_id: str
    resource_type: str
    resource_id: str
    display_summary: str | None = None


Citation = Annotated[
    Union[SourceCitation, GuidelineCitation, PatientChartCitation],
    Field(discriminator="source_type"),
]
"""Discriminated-union of the three citation types, keyed on ``source_type``.

Use this wherever a wire-shape carries a citation whose origin (extracted
document vs. retrieval chunk vs. patient chart) is determined at runtime.
Concrete types are still used at construction sites where the origin is
known statically (the extractor builds ``SourceCitation`` directly).
"""


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
