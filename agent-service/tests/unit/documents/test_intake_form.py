"""Round-trip tests for the intake_form schema."""

from __future__ import annotations

from clinical_copilot.documents.schemas.citation import ExtractedField, SourceCitation
from clinical_copilot.documents.schemas.intake_form import (
    ActiveProblem,
    FamilyHistoryEntry,
    IntakeFormFacts,
    ReportedAllergy,
    ReportedMedication,
    TobaccoStatus,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def _cite(text: str, page: int = 1) -> SourceCitation:
    return SourceCitation(
        document_id="intake-1",
        page=page,
        bbox=(0.1, 0.1, 0.4, 0.15),
        confidence=0.9,
        raw_text=text,
    )


def test_typed_intake_round_trip() -> None:
    facts = IntakeFormFacts(
        document_id="intake-1",
        chief_complaint=ExtractedField[str](
            value="Chest pain x 2 days", citation=_cite("Chest pain x 2 days")
        ),
        current_medications=[
            ReportedMedication(
                name=ExtractedField[str](value="metoprolol", citation=_cite("metoprolol")),
                dose=ExtractedField[str](value="50 mg", citation=_cite("50 mg")),
                frequency=ExtractedField[str](value="BID", citation=_cite("BID")),
                rxnorm=ExtractedField[str](value="6918", citation=_cite("6918")),
                started_year=ExtractedField[int](value=2018, citation=_cite("2018")),
                indication=ExtractedField[str](value="HTN", citation=_cite("HTN")),
            )
        ],
        reported_allergies=[
            ReportedAllergy(
                substance=ExtractedField[str](
                    value="amoxicillin", citation=_cite("amoxicillin")
                ),
                reaction=ExtractedField[str](value="rash", citation=_cite("rash")),
                severity=ExtractedField[str](
                    value="Moderate", citation=_cite("Moderate")
                ),
            )
        ],
        active_problems=[
            ActiveProblem(
                condition=ExtractedField[str](
                    value="Type 2 diabetes mellitus",
                    citation=_cite("Type 2 diabetes mellitus"),
                ),
                icd10=ExtractedField[str](value="E11.9", citation=_cite("E11.9")),
                snomed=ExtractedField[str](
                    value="44054006", citation=_cite("44054006")
                ),
                onset_year=ExtractedField[int](value=2018, citation=_cite("2018")),
                status=ExtractedField[str](value="Active", citation=_cite("Active")),
            )
        ],
        family_history=[
            FamilyHistoryEntry(
                relation=ExtractedField[str](value="Father", citation=_cite("Father")),
                condition=ExtractedField[str](
                    value="Myocardial infarction", citation=_cite("MI")
                ),
                onset_age=ExtractedField[int](value=58, citation=_cite("58")),
                status=ExtractedField[str](
                    value="Deceased", citation=_cite("Deceased")
                ),
            )
        ],
        pain_scale=ExtractedField[int](value=6, citation=_cite("6/10")),
        tobacco_status=ExtractedField[TobaccoStatus](
            value=TobaccoStatus.FORMER, citation=_cite("Former smoker")
        ),
        tobacco_pack_years=ExtractedField[float](
            value=12.0, citation=_cite("12 pack-years")
        ),
    )
    reloaded = IntakeFormFacts.model_validate(facts.model_dump())
    assert reloaded == facts
    assert reloaded.current_medications[0].name.value == "metoprolol"
    assert reloaded.current_medications[0].rxnorm is not None
    assert reloaded.current_medications[0].rxnorm.value == "6918"
    assert reloaded.tobacco_status is not None
    assert reloaded.tobacco_status.value is TobaccoStatus.FORMER
    assert reloaded.active_problems[0].icd10 is not None
    assert reloaded.active_problems[0].icd10.value == "E11.9"
    assert reloaded.family_history[0].relation.value == "Father"


def test_nkda_intake_carries_explicit_no_known_allergies() -> None:
    # The discrepancy engine treats "NKDA" as an explicit assertion of
    # no known allergies, distinct from an empty allergy list (which
    # means the form was silent on the question).
    facts = IntakeFormFacts(
        document_id="intake-1",
        chief_complaint=ExtractedField[str](
            value="Annual physical", citation=_cite("Annual physical")
        ),
        reported_allergies=[
            ReportedAllergy(
                substance=ExtractedField[str](value="NKDA", citation=_cite("NKDA")),
            )
        ],
    )
    assert facts.reported_allergies[0].substance.value == "NKDA"
    assert len(facts.current_medications) == 0


def test_partial_intake_can_abstain_per_field() -> None:
    facts = IntakeFormFacts(
        document_id="intake-1",
        chief_complaint=ExtractedField[str](
            abstain_reason=RuntimeAbstainReason.NO_DATA
        ),
    )
    assert facts.chief_complaint.value is None
    assert facts.chief_complaint.abstain_reason is RuntimeAbstainReason.NO_DATA
    assert len(facts.active_problems) == 0
    assert len(facts.family_history) == 0
    assert facts.tobacco_status is None
