"""Round-trip tests for the lab_pdf schema."""

from __future__ import annotations

from datetime import date

from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.lab_pdf import LabObservation, LabPdfFacts
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def _cite(text: str) -> SourceCitation:
    return SourceCitation(
        document_id="lab-1",
        page=1,
        bbox=(0.1, 0.1, 0.2, 0.15),
        confidence=0.95,
        raw_text=text,
    )


def test_lab_pdf_facts_round_trip_with_full_observation() -> None:
    obs = LabObservation(
        code=ExtractedField[str](value="2345-7", citation=_cite("2345-7")),
        display=ExtractedField[str](value="Glucose", citation=_cite("Glucose")),
        value=ExtractedField[float](value=142.0, citation=_cite("142")),
        unit=ExtractedField[str](value="mg/dL", citation=_cite("mg/dL")),
        effective_date=ExtractedField[date](
            value=date(2025, 11, 12), citation=_cite("11/12/2025")
        ),
        reference_low=ExtractedField[float](value=70.0, citation=_cite("70")),
        reference_high=ExtractedField[float](value=99.0, citation=_cite("99")),
        flag=ExtractedField[str](value="H", citation=_cite("H")),
    )
    facts = LabPdfFacts(document_id="lab-1", observations=[obs])

    dumped = facts.model_dump()
    reloaded = LabPdfFacts.model_validate(dumped)
    assert reloaded == facts
    assert reloaded.observations[0].value.value == 142.0


def test_lab_pdf_facts_supports_abstained_fields() -> None:
    # Mirror what the extractor emits when a field is illegible.
    obs = LabObservation(
        code=ExtractedField[str](abstain_reason=RuntimeAbstainReason.NO_DATA),
        display=ExtractedField[str](value="Sodium", citation=_cite("Sodium")),
        value=ExtractedField[float](abstain_reason=RuntimeAbstainReason.LOW_CONFIDENCE),
        unit=ExtractedField[str](value="mmol/L", citation=_cite("mmol/L")),
        effective_date=ExtractedField[date](
            value=date(2025, 11, 12), citation=_cite("11/12/2025")
        ),
    )
    facts = LabPdfFacts(document_id="lab-1", observations=[obs])
    assert facts.observations[0].value.abstain_reason is RuntimeAbstainReason.LOW_CONFIDENCE
    assert facts.observations[0].reference_low is None  # optional fields
