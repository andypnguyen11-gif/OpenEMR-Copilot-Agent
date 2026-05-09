"""Drift-guard invariant: a typed Citation's canonical id must match the
canonical id of the source it represents.

The plan (Option 1, Pre-flight #5) keeps ``source_id`` as the verifier's
join key and treats ``citation`` as additive display metadata. To prevent
the two from drifting, every site that produces a typed citation must
make the citation's ``field_or_chunk_id`` match the source-side canonical
id:

* :class:`PatientChartCitation` — ``field_or_chunk_id`` equals the
  ``ChartPackRecord.source_id`` (the ``ResourceType/{id}`` shape that the
  verifier joins on).
* :class:`GuidelineCitation` — ``field_or_chunk_id`` equals the
  retriever's ``RetrievedChunk.chunk_id``.
* :class:`SourceCitation` — invariant deferred. The extractor's
  schema-walk path (``observations[0].value``) is the canonical id; the
  source side has no separate string to join against until the bbox
  overlay (PR 5) lands.

These tests are CI invariants (NOT Pydantic ``model_validator``s) — a
runtime validator that fails would abort a clinician-facing response and
the cost of that asymmetry isn't worth the marginal safety. CI catches
drift before merge, which is sufficient.
"""

from __future__ import annotations

from clinical_copilot.corpus.retriever import RetrievedChunk
from clinical_copilot.documents.schemas.citation import GuidelineCitation
from clinical_copilot.orchestrator.chart_pack import ChartPackRecord
from clinical_copilot.orchestrator.workers.evidence_retriever import _chunk_to_dict
from clinical_copilot.tools.records import LabRecord


def _lab(source_id: str) -> LabRecord:
    return LabRecord(
        source_id=source_id,
        code="2345-7",
        display="Glucose",
        value="142",
        unit="mg/dL",
        observed_on="2026-04-15",
    )


def test_patient_chart_citation_canonical_id_matches_source_id() -> None:
    """ChartPackRecord.to_citation() must bind field_or_chunk_id to the
    record's source_id verbatim — that's the verifier's join key."""

    source_id = "Observation/123"
    record = ChartPackRecord(
        source_id=source_id,
        resource_type="Observation",
        topic="labs",
        summary="Glucose 142 mg/dL on 2026-04-15",
        record=_lab(source_id),
    )
    citation = record.to_citation()
    assert citation.field_or_chunk_id == record.source_id


def test_patient_chart_citation_invariant_holds_for_every_topic() -> None:
    """Spot-check across multiple ``resource_type`` shapes — the invariant
    is a per-record contract, not topic-specific."""

    for source_id, resource_type in [
        ("Observation/123", "Observation"),
        ("MedicationRequest/9", "MedicationRequest"),
        ("Condition/5", "Condition"),
        ("AllergyIntolerance/77", "AllergyIntolerance"),
    ]:
        record = ChartPackRecord(
            source_id=source_id,
            resource_type=resource_type,
            topic="labs",  # topic doesn't affect citation
            summary=f"{resource_type} summary",
            record=_lab(source_id),
        )
        citation = record.to_citation()
        assert citation.field_or_chunk_id == source_id, (
            f"drift: {resource_type}/{source_id} citation field_or_chunk_id "
            f"= {citation.field_or_chunk_id!r}, expected {source_id!r}"
        )


def test_guideline_citation_canonical_id_matches_chunk_id() -> None:
    """evidence_retriever._chunk_to_dict must build the GuidelineCitation
    so its field_or_chunk_id equals the chunk's chunk_id — the
    canonical retrieval-side id."""

    chunk = RetrievedChunk(
        chunk_id="acc-2024-stable-cad#chunk-7",
        source_doc_id="acc-2024-stable-cad",
        title="Stable CAD management",
        source="ACC",
        source_url="https://example.test/acc-2024-stable-cad.pdf",
        text="Class IIa: consider beta-blocker in patients with prior MI...",
        score=0.83,
    )
    chunk_dict = _chunk_to_dict(chunk)
    citation = GuidelineCitation.model_validate(chunk_dict["citation"])
    assert citation.field_or_chunk_id == chunk.chunk_id
    assert citation.chunk_id == chunk.chunk_id
