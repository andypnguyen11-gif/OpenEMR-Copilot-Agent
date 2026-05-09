"""Unit tests for the chart-pack pre-fetch module.

The module fans out across six topics via a
:class:`PatientScopedToolRegistry`, projects each ``ToolResult`` into
prompt-shaped records, and reports per-topic success / failure. These
tests use a stub registry so the suite stays sync-friendly and the
prompt-block shape is locked against regressions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.chart_pack import (
    DEFAULT_PER_TOPIC_CAP,
    DEFAULT_TOPICS,
    ChartPack,
    ChartPackRecord,
    build_chart_pack,
)
from clinical_copilot.tools.base import (
    FhirAuthorizationDeniedError,
    UnauthorizedToolCallError,
)
from clinical_copilot.tools.records import (
    AllergyRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
    ToolResult,
    VisitRecord,
)

# --------------------------------------------------------------- helpers


@dataclass
class _StubScopedRegistry:
    """Stub of :class:`PatientScopedToolRegistry`.

    Exposes the same ``dispatch(name, claims, request_id)`` surface
    so :func:`build_chart_pack` cannot tell the difference. Returns
    pre-staged :class:`ToolResult`/exception per tool name and
    records every dispatch for assertions.
    """

    results: dict[str, ToolResult] = field(default_factory=dict)
    exceptions: dict[str, Exception] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    """Each entry is ``(tool_name, request_id)`` so order can be asserted."""

    def dispatch(
        self,
        name: str,
        *,
        claims: ClinicianClaims,
        request_id: str,
    ) -> ToolResult:
        self.calls.append((name, request_id))
        if name in self.exceptions:
            raise self.exceptions[name]
        if name in self.results:
            return self.results[name]
        # Unconfigured tool name: empty result. Mirrors the production
        # registry's behavior of returning whatever the tool produced —
        # the chart pack treats no records as a successful empty fetch.
        return ToolResult(tool_name=name, patient_id=claims.patient_id, records=[])


def _claims(patient_id: str = "90109") -> ClinicianClaims:
    return ClinicianClaims(
        user_id="1",
        role=Role.PHYSICIAN,
        patient_id=patient_id,
        scopes=("patient/Observation.read",),
        nonce="n",
        jti="j",
    )


def _lab(source_id: str, display: str, value: str, observed_on: str) -> LabRecord:
    return LabRecord(
        source_id=source_id,
        code="codex",
        display=display,
        value=value,
        unit="mIU/L",
        observed_on=observed_on,
    )


def _med(source_id: str, name: str, started_on: str | None = None) -> MedicationRecord:
    return MedicationRecord(
        source_id=source_id,
        name=name,
        dose="50 mcg",
        status="active",
        started_on=started_on,
    )


def _problem(source_id: str, display: str) -> ProblemRecord:
    return ProblemRecord(
        source_id=source_id,
        code="E03.9",
        display=display,
        onset_date="2026-04-05",
        status="active",
    )


def _allergy(source_id: str, substance: str) -> AllergyRecord:
    return AllergyRecord(
        source_id=source_id,
        substance=substance,
        reaction="hives",
        severity="moderate",
    )


def _visit(source_id: str, encounter_type: str, visited_on: str) -> VisitRecord:
    return VisitRecord(
        source_id=source_id,
        encounter_type=encounter_type,
        visited_on=visited_on,
        chief_complaint="follow-up",
    )


def _note(source_id: str, body: str) -> NoteRecord:
    return NoteRecord(
        source_id=source_id,
        note_date="2026-04-05",
        author="Dr. Smith",
        body=body,
    )


# --------------------------------------------------------------- tests


def test_build_chart_pack_collects_records_from_every_topic() -> None:
    """Happy path: all six topics return records; pack carries them
    flat with topic-bucketed prompt rendering."""

    registry = _StubScopedRegistry(
        results={
            "get_labs": ToolResult(
                tool_name="get_labs",
                patient_id="90109",
                records=[_lab("Observation/1", "TSH", "6.73", "2026-04-05")],
            ),
            "get_meds": ToolResult(
                tool_name="get_meds",
                patient_id="90109",
                records=[_med("MedicationRequest/8", "levothyroxine", "2026-04-10")],
            ),
            "get_problems": ToolResult(
                tool_name="get_problems",
                patient_id="90109",
                records=[_problem("Condition/2", "Hypothyroidism")],
            ),
            "get_allergies": ToolResult(
                tool_name="get_allergies",
                patient_id="90109",
                records=[_allergy("AllergyIntolerance/3", "penicillin")],
            ),
            "get_visits": ToolResult(
                tool_name="get_visits",
                patient_id="90109",
                records=[_visit("Encounter/4", "office", "2026-04-05")],
            ),
            "get_notes": ToolResult(
                tool_name="get_notes",
                patient_id="90109",
                records=[_note("DocumentReference/5", "Progress note body")],
            ),
        }
    )

    pack = asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-1",
        )
    )

    assert isinstance(pack, ChartPack)
    assert pack.patient_id == "90109"
    assert {r.topic for r in pack.records} == set(DEFAULT_TOPICS)
    assert pack.fetched_topics == DEFAULT_TOPICS
    assert pack.failed_topics == ()
    # Every record carries a usable source_id and resource_type derived
    # from the slash-prefix.
    assert {r.resource_type for r in pack.records} == {
        "Observation",
        "MedicationRequest",
        "Condition",
        "AllergyIntolerance",
        "Encounter",
        "DocumentReference",
    }
    assert "Observation/1" in pack.source_ids()
    assert not pack.is_empty()


def test_build_chart_pack_dispatches_one_call_per_topic() -> None:
    """Each topic maps to exactly one tool dispatch — no duplicates,
    no extras. ``request_id`` is forwarded on every call."""

    registry = _StubScopedRegistry()  # all topics return empty
    asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-2",
        )
    )

    tool_names = {name for name, _ in registry.calls}
    assert tool_names == {
        "get_labs",
        "get_meds",
        "get_problems",
        "get_allergies",
        "get_visits",
        "get_notes",
    }
    assert all(rid == "req-2" for _, rid in registry.calls)


def test_failed_topic_lands_in_failed_topics_and_other_topics_succeed() -> None:
    """A non-auth tool exception on one topic is logged and the topic
    is recorded as failed. Other topics still produce records."""

    registry = _StubScopedRegistry(
        results={
            "get_labs": ToolResult(
                tool_name="get_labs",
                patient_id="90109",
                records=[_lab("Observation/1", "TSH", "6.73", "2026-04-05")],
            ),
        },
        exceptions={
            "get_meds": RuntimeError("upstream 500"),
        },
    )

    pack = asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-3",
        )
    )

    assert "labs" in pack.fetched_topics
    assert "meds" in pack.failed_topics
    # Labs records survive even though meds tanked.
    assert any(r.source_id == "Observation/1" for r in pack.records)


def test_unauthorized_tool_call_propagates() -> None:
    """An :class:`UnauthorizedToolCallError` from any topic re-raises
    rather than degrading silently — patient-mismatch is a wiring bug,
    not partial data."""

    registry = _StubScopedRegistry(
        exceptions={
            "get_labs": UnauthorizedToolCallError(
                "get_labs",
                requested_patient_id="99999",
            ),
        },
    )

    with pytest.raises(UnauthorizedToolCallError):
        asyncio.run(
            build_chart_pack(
                scoped_registry=registry,
                claims=_claims(),
                request_id="req-4",
            )
        )


def test_fhir_authorization_denied_propagates() -> None:
    """ACL denial from FHIR also re-raises — same trust-fail policy."""

    registry = _StubScopedRegistry(
        exceptions={
            "get_visits": FhirAuthorizationDeniedError("403 from server"),
        },
    )

    with pytest.raises(FhirAuthorizationDeniedError):
        asyncio.run(
            build_chart_pack(
                scoped_registry=registry,
                claims=_claims(),
                request_id="req-5",
            )
        )


def test_per_topic_cap_truncates_to_most_recent() -> None:
    """``per_topic_cap`` keeps the tail of each tool's records and
    reverses so newest-first lands in the prompt."""

    labs = [
        _lab(f"Observation/{n}", "TSH", str(n), f"2026-04-0{n}")
        for n in range(1, 9)
    ]
    registry = _StubScopedRegistry(
        results={
            "get_labs": ToolResult(
                tool_name="get_labs",
                patient_id="90109",
                records=labs,
            ),
        },
    )

    pack = asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-6",
            per_topic_cap=3,
        )
    )

    lab_records = [r for r in pack.records if r.topic == "labs"]
    assert [r.source_id for r in lab_records] == [
        "Observation/8",
        "Observation/7",
        "Observation/6",
    ]


def test_default_per_topic_cap_is_five() -> None:
    """Documented default — the prompt-cost ceiling lives here."""

    assert DEFAULT_PER_TOPIC_CAP == 5


def test_to_prompt_block_renders_section_per_topic_with_source_ids() -> None:
    """The prompt block is markdown-shaped, has one section per non-
    empty topic, and ends every record line with a ``source_id=``
    token the LLM is told to copy verbatim."""

    sample_lab = _lab("Observation/1", "TSH", "6.73", "2026-04-05")
    sample_med = _med("MedicationRequest/8", "levothyroxine", "2026-04-10")
    pack = ChartPack(
        patient_id="90109",
        records=(
            ChartPackRecord(
                source_id="Observation/1",
                resource_type="Observation",
                topic="labs",
                summary="TSH: 6.73 mIU/L (observed_on=2026-04-05)",
                record=sample_lab,
            ),
            ChartPackRecord(
                source_id="MedicationRequest/8",
                resource_type="MedicationRequest",
                topic="meds",
                summary="levothyroxine 50 mcg (started=2026-04-10, status=active)",
                record=sample_med,
            ),
        ),
        fetched_topics=("labs", "meds"),
        failed_topics=(),
    )

    block = pack.to_prompt_block()

    assert block.startswith("<patient_chart>")
    assert block.endswith("</patient_chart>")
    assert "## Recent labs (1 records)" in block
    assert "## Active medications (1 records)" in block
    assert "source_id=Observation/1" in block
    assert "source_id=MedicationRequest/8" in block


def test_empty_pack_renders_empty_prompt_block() -> None:
    """A pack with zero records produces an empty string so callers
    can decide to skip injection entirely."""

    pack = ChartPack(
        patient_id="90109",
        records=(),
        fetched_topics=DEFAULT_TOPICS,
        failed_topics=(),
    )

    assert pack.to_prompt_block() == ""
    assert pack.is_empty()
    assert pack.source_ids() == frozenset()


def test_topics_arg_restricts_fetch_to_listed_topics() -> None:
    """Caller-supplied ``topics`` overrides ``DEFAULT_TOPICS``."""

    registry = _StubScopedRegistry()
    asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-7",
            topics=("labs", "meds"),
        )
    )

    tool_names = {name for name, _ in registry.calls}
    assert tool_names == {"get_labs", "get_meds"}


def test_record_without_source_id_is_dropped_silently() -> None:
    """Defense-in-depth: a malformed record (missing source_id) is
    skipped rather than crashing the pack build."""

    @dataclass(frozen=True)
    class _Bogus:
        source_id: str = ""  # empty triggers the drop branch
        display: str = "bogus"
        value: str = "?"
        unit: str | None = None
        observed_on: str = "?"

    # Bypass Pydantic validation — we want a record-shaped object that
    # ToolResult would reject. Build the registry to return one
    # constructed via model_construct so we can simulate the malformed
    # case without changing record schemas.
    good = _lab("Observation/1", "TSH", "6.73", "2026-04-05")
    bad = LabRecord.model_construct(
        source_id="",
        code="x",
        display="bogus",
        value="?",
        unit=None,
        observed_on="?",
        reference_range=None,
    )

    registry = _StubScopedRegistry(
        results={
            "get_labs": ToolResult(
                tool_name="get_labs",
                patient_id="90109",
                records=[bad, good],
            ),
        },
    )
    pack = asyncio.run(
        build_chart_pack(
            scoped_registry=registry,
            claims=_claims(),
            request_id="req-8",
        )
    )

    sources = [r.source_id for r in pack.records if r.topic == "labs"]
    assert sources == ["Observation/1"]


# --------------------------------------------------- ChartPackRecord.to_citation


def test_to_citation_builds_patient_chart_citation_for_well_formed_source_id() -> None:
    record = ChartPackRecord(
        source_id="Observation/123",
        resource_type="Observation",
        topic="labs",
        summary="TSH 6.73 mIU/L (observed_on=2026-04-05)",
        record=_lab("Observation/123", "TSH", "6.73", "2026-04-05"),
    )
    citation = record.to_citation()
    assert citation.source_type == "patient_chart"
    assert citation.field_or_chunk_id == "Observation/123"
    assert citation.resource_type == "Observation"
    assert citation.resource_id == "123"
    assert citation.display_summary == "TSH 6.73 mIU/L (observed_on=2026-04-05)"


def test_to_citation_falls_back_to_full_source_id_when_no_slash_present() -> None:
    """Defensive: a source_id missing the canonical "Type/{id}" separator
    still produces a non-empty resource_id rather than crashing the
    response build for a malformed chart-pack producer output."""

    record = ChartPackRecord(
        source_id="MedicationRequest42",  # no slash
        resource_type="MedicationRequest",
        topic="meds",
        summary="levothyroxine",
        record=_med("MedicationRequest42", "levothyroxine", "2026-04-10"),
    )
    citation = record.to_citation()
    assert citation.resource_id == "MedicationRequest42"
    assert citation.field_or_chunk_id == "MedicationRequest42"
