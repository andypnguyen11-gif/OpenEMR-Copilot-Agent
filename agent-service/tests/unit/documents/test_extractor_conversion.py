"""Offline tests for the raw → ExtractedField conversion helpers.

The full W2-03 plan covers extractor-level tests with cassetted
Anthropic responses; for the demo cut we cover only the pure
conversion functions, which is where a confidence-threshold or
abstain-reason regression would land first. Live extraction is
exercised via the local-ingest CLI against fixture PDFs (see README
demo path).
"""

from __future__ import annotations

from clinical_copilot.documents.extractor import (
    CONFIDENCE_THRESHOLD,
    RawCitation,
    RawIntakeExtraction,
    RawLabObservation,
    RawReportedAllergy,
    _to_intake_facts,
    _to_lab_observation,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def _raw_cite(text: str = "stub") -> RawCitation:
    return RawCitation(page=1, bbox=[0.1, 0.2, 0.3, 0.25], raw_text=text)


def test_high_confidence_lab_obs_converts_to_extracted_fields() -> None:
    raw = RawLabObservation(
        display="Glucose",
        code="2345-7",
        value=142.0,
        unit="mg/dL",
        effective_date="2025-11-12",
        reference_low=70.0,
        reference_high=99.0,
        flag="H",
        confidence=0.95,
        citation=_raw_cite("Glucose 142 mg/dL"),
    )
    obs = _to_lab_observation(document_id="doc-1", raw=raw, index=0)
    assert obs.value.value == 142.0
    assert obs.value.citation is not None
    assert obs.value.abstain_reason is None
    assert obs.flag is not None
    assert obs.flag.value == "H"


def test_low_confidence_lab_obs_drops_required_fields_to_low_confidence() -> None:
    raw = RawLabObservation(
        display="Sodium",
        value=139.0,
        unit="mmol/L",
        effective_date="2025-11-12",
        confidence=CONFIDENCE_THRESHOLD - 0.05,
        citation=_raw_cite("Sodium 139 mmol/L"),
    )
    obs = _to_lab_observation(document_id="doc-1", raw=raw, index=0)
    assert obs.value.value is None
    assert obs.value.abstain_reason is RuntimeAbstainReason.LOW_CONFIDENCE
    assert obs.display.abstain_reason is RuntimeAbstainReason.LOW_CONFIDENCE


def test_missing_required_lab_field_becomes_no_data() -> None:
    raw = RawLabObservation(
        display="Sodium",
        value=None,
        unit="mmol/L",
        effective_date="2025-11-12",
        confidence=0.95,
        citation=_raw_cite("Sodium illegible"),
    )
    obs = _to_lab_observation(document_id="doc-1", raw=raw, index=0)
    assert obs.value.value is None
    assert obs.value.abstain_reason is RuntimeAbstainReason.NO_DATA


def test_unparseable_date_drops_to_no_data() -> None:
    raw = RawLabObservation(
        display="Glucose",
        value=142.0,
        unit="mg/dL",
        effective_date="11/12/2025",  # not ISO
        confidence=0.95,
        citation=_raw_cite("Glucose 142"),
    )
    obs = _to_lab_observation(document_id="doc-1", raw=raw, index=0)
    assert obs.effective_date.abstain_reason is RuntimeAbstainReason.NO_DATA


def test_missing_optional_lab_field_is_none_not_abstain() -> None:
    # Optional fields (reference_low/high, flag) that the report omits
    # render as None — they shouldn't take up an abstain slot, since
    # there's nothing to abstain *from*.
    raw = RawLabObservation(
        display="Glucose",
        value=142.0,
        unit="mg/dL",
        effective_date="2025-11-12",
        confidence=0.95,
        citation=_raw_cite("Glucose 142"),
    )
    obs = _to_lab_observation(document_id="doc-1", raw=raw, index=0)
    assert obs.reference_low is None
    assert obs.reference_high is None
    assert obs.flag is None


def test_intake_with_nkda_emits_single_allergy_entry() -> None:
    raw = RawIntakeExtraction(
        chief_complaint="Annual physical",
        chief_complaint_confidence=0.95,
        chief_complaint_citation=_raw_cite("Annual physical"),
        reported_allergies=[
            RawReportedAllergy(
                substance="NKDA",
                confidence=0.95,
                citation=_raw_cite("NKDA"),
            )
        ],
    )
    facts = _to_intake_facts(document_id="intake-1", raw=raw)
    assert len(facts.reported_allergies) == 1
    assert facts.reported_allergies[0].substance.value == "NKDA"


def test_intake_with_no_chief_complaint_data_abstains_no_data() -> None:
    raw = RawIntakeExtraction()
    facts = _to_intake_facts(document_id="intake-1", raw=raw)
    assert facts.chief_complaint.abstain_reason is RuntimeAbstainReason.NO_DATA
    assert facts.pain_scale is None
