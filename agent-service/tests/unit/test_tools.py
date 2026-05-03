"""Unit tests for the M1 tool layer.

Two paths per tool:

* **Happy path** — caller's :class:`ClinicianClaims` are scoped to the
  fixture patient and carry the tool's required scope. Tool returns
  the expected typed records and the audit log stays empty (M1 only
  audits denials; success-side audit lands in PR 19).
* **RBAC denial path** — caller's ``patient_id`` does not match the
  fixture patient (the out-of-panel sentinel #999 is the eval target).
  Tool raises :class:`UnauthorizedToolCallError` and writes exactly one
  audit row whose hash is over the *requested* patient ID, not the
  caller's session patient.

The tests use a process-local fake :class:`AuditLogWriter` so the
fail-closed contract of the real writer is exercised end-to-end without
a Postgres dependency.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.discrepancy.cache import DiscrepancyCache
from clinical_copilot.discrepancy.chart_provider import FixtureChartProvider
from clinical_copilot.discrepancy.engine import DiscrepancyEngine
from clinical_copilot.discrepancy.rules import DEFAULT_PACK_PATHS, DEFAULT_REGISTRY
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.impl import (
    GetAllergiesTool,
    GetFlagsTool,
    GetLabsTool,
    GetMedsTool,
    GetNotesTool,
    GetProblemsTool,
    GetVisitsTool,
)
from clinical_copilot.tools.registry import ToolRegistry, UnknownToolError

AUDIT_SALT = "test-salt"


class _RecordingAuditWriter(AuditLogWriter):
    """Drop-in for the real writer that records events to a list.

    Subclassing the production class (rather than ducking the type)
    keeps the type checker happy: every call site of
    :class:`AuditLogWriter` accepts this in production code without
    casts.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore.from_file()


@pytest.fixture
def audit() -> _RecordingAuditWriter:
    return _RecordingAuditWriter()


def _claims_for(patient_id: str, *, scopes: list[str] | None = None) -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id=patient_id,
        scopes=scopes if scopes is not None else _ALL_SCOPES,
        nonce="n-test",
        jti=f"jti-{patient_id}",
    )


_ALL_SCOPES = [
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
]


_TOOL_SPECS: tuple[tuple[type[Any], str, str], ...] = (
    (GetProblemsTool, "get_problems", "Condition/p101-cond-1"),
    (GetMedsTool, "get_meds", "MedicationRequest/p101-med-1"),
    (GetAllergiesTool, "get_allergies", "AllergyIntolerance/p101-allergy-1"),
    (GetLabsTool, "get_labs", "Observation/p101-lab-1"),
    (GetVisitsTool, "get_visits", "Encounter/p101-enc-1"),
    (GetNotesTool, "get_notes", "DocumentReference/p101-note-1"),
)


@pytest.mark.parametrize(("tool_cls", "tool_name", "expected_source_id"), _TOOL_SPECS)
def test_tool_happy_path_returns_records_with_source_id(
    tool_cls: type[Any],
    tool_name: str,
    expected_source_id: str,
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    tool = tool_cls(store=store, audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims_for("101")

    result = tool.execute(
        claims=claims,
        patient_id="101",
        request_id="req-happy",
    )

    assert result.tool_name == tool_name
    assert result.patient_id == "101"
    assert len(result.records) >= 1
    # Every record carries a stable source_id — the citation-existence
    # join in the verification middleware depends on this invariant.
    assert all(record.source_id for record in result.records)
    assert any(record.source_id == expected_source_id for record in result.records)
    assert audit.events == []


@pytest.mark.parametrize(("tool_cls", "tool_name", "_expected"), _TOOL_SPECS)
def test_tool_rbac_denial_writes_audit_row_and_raises(
    tool_cls: type[Any],
    tool_name: str,
    _expected: str,
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    tool = tool_cls(store=store, audit=audit, audit_salt=AUDIT_SALT)
    # Session is scoped to patient 101; the model attempts to fetch the
    # out-of-panel sentinel 999.
    claims = _claims_for("101")

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(
            claims=claims,
            patient_id="999",
            request_id="req-deny",
        )

    assert excinfo.value.tool_name == tool_name
    assert excinfo.value.requested_patient_id == "999"

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.user_id == "dr-patel"
    assert event.role == "physician"
    assert event.action == "UNAUTHORIZED"
    assert event.resource_type == tool_name
    assert event.request_id == "req-deny"
    # Hash is over the *requested* (out-of-panel) patient_id — that's
    # the target of the denial, not the caller's session patient.
    assert event.patient_id_hash == hash_patient_id("999", salt=AUDIT_SALT)


def test_get_flags_returns_safety_conflict_for_p104(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    chart_provider = FixtureChartProvider(store)
    engine = DiscrepancyEngine.from_yaml(DEFAULT_PACK_PATHS, DEFAULT_REGISTRY)
    cache = DiscrepancyCache(chart_provider=chart_provider, engine=engine)
    tool = GetFlagsTool(
        cache=cache,
        audit=audit,
        audit_salt=AUDIT_SALT,
    )
    claims = _claims_for("104")

    result = tool.execute(claims=claims, patient_id="104", request_id="req-flags")

    safety_flags = [
        flag
        for flag in result.records
        if getattr(flag, "rule_id", None) == "allergen_med_safety_conflict"
    ]
    assert len(safety_flags) == 1
    assert getattr(safety_flags[0], "category", None) == "safety"


def test_missing_required_scope_denies_even_when_patient_matches(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    tool = GetMedsTool(store=store, audit=audit, audit_salt=AUDIT_SALT)
    # Patient ID matches but the medications scope is missing — the
    # denial path is still taken.
    claims = _claims_for("101", scopes=["system/Condition.read"])

    with pytest.raises(UnauthorizedToolCallError):
        tool.execute(claims=claims, patient_id="101", request_id="req-noscope")

    assert len(audit.events) == 1
    assert audit.events[0].action == "UNAUTHORIZED"
    assert audit.events[0].resource_type == "get_meds"


def test_unknown_patient_returns_empty_records(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    # An unknown patient_id is a "no records of this type" surface, not
    # a fixture-load error — the abstention layer turns this into NO_DATA
    # downstream. RBAC still gates: claims must match the requested ID.
    tool = GetProblemsTool(store=store, audit=audit, audit_salt=AUDIT_SALT)
    claims = _claims_for("not-in-fixture")

    result = tool.execute(
        claims=claims,
        patient_id="not-in-fixture",
        request_id="req-empty",
    )

    assert result.records == []


def test_registry_dispatches_by_name(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    registry = ToolRegistry.from_fixture(
        store=store,
        audit=audit,
        audit_salt=AUDIT_SALT,
    )
    claims = _claims_for("103")

    result = registry.dispatch(
        "get_flags",
        claims=claims,
        patient_id="103",
        request_id="req-dispatch",
    )

    assert result.tool_name == "get_flags"
    assert any(
        getattr(record, "rule_id", None) == "med_vs_note_conflict" for record in result.records
    )


def test_registry_unknown_tool_raises(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    registry = ToolRegistry.from_fixture(
        store=store,
        audit=audit,
        audit_salt=AUDIT_SALT,
    )
    claims = _claims_for("101")

    with pytest.raises(UnknownToolError):
        registry.dispatch(
            "get_nonexistent",
            claims=claims,
            patient_id="101",
            request_id="req-bad",
        )


def test_registry_anthropic_schemas_cover_all_tools(
    store: FixtureStore,
    audit: _RecordingAuditWriter,
) -> None:
    registry = ToolRegistry.from_fixture(
        store=store,
        audit=audit,
        audit_salt=AUDIT_SALT,
    )
    schemas = registry.anthropic_schemas()

    names: list[str] = [str(schema["name"]) for schema in schemas]
    assert names == sorted(names)
    assert set(names) == {
        "get_allergies",
        "get_flags",
        "get_labs",
        "get_meds",
        "get_notes",
        "get_problems",
        "get_visits",
    }
    # Every schema declares the patient_id input contract.
    for schema in schemas:
        input_schema = schema["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema["required"] == ["patient_id"]
