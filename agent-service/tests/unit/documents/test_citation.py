"""Tests for SourceCitation + ExtractedField + the discriminated Citation
union (PRD2 §6 + Appendix A.1).
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from clinical_copilot.documents.schemas.citation import (
    Citation,
    CitationSourceType,
    ExtractedField,
    GuidelineCitation,
    PatientChartCitation,
    SourceCitation,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def _valid_citation(field_or_chunk_id: str = "observations[0].value") -> SourceCitation:
    return SourceCitation(
        document_id="doc-1",
        page=1,
        bbox=(0.10, 0.20, 0.30, 0.25),
        confidence=0.92,
        raw_text="142",
        field_or_chunk_id=field_or_chunk_id,
    )


def _valid_guideline_citation() -> GuidelineCitation:
    return GuidelineCitation(
        field_or_chunk_id="chunk-acc-2024-stable-cad-7",
        source_doc_id="acc-2024-stable-cad",
        chunk_id="chunk-acc-2024-stable-cad-7",
        source_url="https://example.test/acc-2024-stable-cad.pdf",
        confidence=0.83,
        raw_text="Class IIa: consider beta-blocker in patients with prior MI...",
    )


def _valid_chart_citation() -> PatientChartCitation:
    return PatientChartCitation(
        field_or_chunk_id="Observation/123",
        resource_type="Observation",
        resource_id="123",
        display_summary="Glucose 142 mg/dL on 2026-04-15",
    )


class TestSourceCitation:
    def test_valid_citation_round_trips(self) -> None:
        c = _valid_citation()
        assert c.page == 1
        assert c.bbox == (0.10, 0.20, 0.30, 0.25)
        assert c.source_type == "extracted_document"
        assert c.field_or_chunk_id == "observations[0].value"

    def test_serialised_round_trip_preserves_discriminator(self) -> None:
        original = _valid_citation()
        rebuilt = SourceCitation.model_validate(original.model_dump())
        assert rebuilt == original
        assert rebuilt.source_type == "extracted_document"

    def test_field_or_chunk_id_is_required(self) -> None:
        # PR 1b dropped the transitional empty-string default; constructing
        # a SourceCitation without a leaf path is a contract violation now
        # and must raise rather than silently emit ``field_or_chunk_id=""``.
        with pytest.raises(ValidationError):
            SourceCitation(
                document_id="doc-1",
                page=1,
                bbox=(0.0, 0.0, 0.1, 0.1),
                confidence=0.9,
                raw_text="x",
            )

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


class TestGuidelineCitation:
    def test_round_trip_preserves_fields(self) -> None:
        original = _valid_guideline_citation()
        rebuilt = GuidelineCitation.model_validate(original.model_dump())
        assert rebuilt == original
        assert rebuilt.source_type == "guideline"
        assert rebuilt.field_or_chunk_id == original.chunk_id

    def test_source_url_is_optional(self) -> None:
        c = GuidelineCitation(
            field_or_chunk_id="chunk-x",
            source_doc_id="doc-x",
            chunk_id="chunk-x",
            confidence=0.5,
            raw_text="...",
        )
        assert c.source_url is None

    def test_confidence_outside_unit_interval_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GuidelineCitation(
                field_or_chunk_id="chunk-x",
                source_doc_id="doc-x",
                chunk_id="chunk-x",
                confidence=1.5,
                raw_text="...",
            )


class TestPatientChartCitation:
    def test_round_trip_preserves_fields(self) -> None:
        original = _valid_chart_citation()
        rebuilt = PatientChartCitation.model_validate(original.model_dump())
        assert rebuilt == original
        assert rebuilt.source_type == "patient_chart"
        assert rebuilt.field_or_chunk_id == "Observation/123"

    def test_display_summary_is_optional(self) -> None:
        c = PatientChartCitation(
            field_or_chunk_id="MedicationRequest/9",
            resource_type="MedicationRequest",
            resource_id="9",
        )
        assert c.display_summary is None


class TestCitationDiscriminatedUnion:
    """The wire-shape carrier for citations is the discriminated union.

    Round-trip a list containing all three citation shapes through a
    Pydantic model that types the list as ``list[Citation]``; verify the
    discriminator routes each element to the right concrete class.
    """

    def test_mixed_list_round_trips_with_correct_concrete_types(self) -> None:
        original_items: list[Citation] = [
            _valid_citation(),
            _valid_guideline_citation(),
            _valid_chart_citation(),
        ]
        adapter: TypeAdapter[list[Citation]] = TypeAdapter(list[Citation])
        wire = adapter.dump_python(original_items, mode="python")
        rebuilt = adapter.validate_python(wire)

        assert len(rebuilt) == 3
        assert isinstance(rebuilt[0], SourceCitation)
        assert isinstance(rebuilt[1], GuidelineCitation)
        assert isinstance(rebuilt[2], PatientChartCitation)
        assert rebuilt == original_items

    def test_json_round_trip_via_discriminator(self) -> None:
        adapter: TypeAdapter[Citation] = TypeAdapter(Citation)
        for original in (
            _valid_citation(),
            _valid_guideline_citation(),
            _valid_chart_citation(),
        ):
            wire = adapter.dump_json(original)
            rebuilt = adapter.validate_json(wire)
            assert rebuilt == original
            assert type(rebuilt) is type(original)

    def test_extracted_document_payload_rejected_by_guideline_class(self) -> None:
        # GuidelineCitation has source_type=Literal["guideline"] — a payload
        # carrying source_type="extracted_document" must not validate.
        payload = _valid_citation().model_dump()
        with pytest.raises(ValidationError):
            GuidelineCitation.model_validate(payload)

    def test_chart_payload_rejected_by_extracted_document_class(self) -> None:
        payload = _valid_chart_citation().model_dump()
        with pytest.raises(ValidationError):
            SourceCitation.model_validate(payload)

    def test_unknown_source_type_rejected_by_union(self) -> None:
        adapter: TypeAdapter[Citation] = TypeAdapter(Citation)
        bad_payload = {
            "source_type": "made_up",
            "field_or_chunk_id": "x",
        }
        with pytest.raises(ValidationError):
            adapter.validate_python(bad_payload)


class TestCitationSourceTypeEnum:
    def test_enum_values_match_literal_discriminators(self) -> None:
        # Sanity check that callers using the StrEnum members get strings
        # equal to the Literal values used in the citation classes' fields.
        assert CitationSourceType.EXTRACTED_DOCUMENT.value == "extracted_document"
        assert CitationSourceType.GUIDELINE.value == "guideline"
        assert CitationSourceType.PATIENT_CHART.value == "patient_chart"
