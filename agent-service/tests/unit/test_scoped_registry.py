"""Unit tests for :class:`PatientScopedToolRegistry`.

The view is the structural defense against cross-patient tool calls:
each agent request binds the registry to ``claims.patient_id`` once,
and every dispatch from then on supplies the bound id. The LLM has no
``patient_id`` argument on the tool surface, so a prompt-injection
probe that puts ``patient_id=999`` in ``tool_use.input`` is a no-op —
the dispatcher ignores the field and the underlying tool still reads
records for the bound patient (101 in these tests).

The view also carries a defense-in-depth check: ``claims.patient_id``
must equal the bound id. A divergence is a wiring bug upstream — the
orchestrator should always build the view from the same claims it
later passes to ``dispatch``. The test pinned here makes that bug
fail closed before the underlying tool runs.
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import (
    PatientScopedToolRegistry,
    ToolRegistry,
)

AUDIT_SALT = "test-salt"

_ALL_SCOPES = [
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/DocumentReference.read",
]


class _RecordingAudit(AuditLogWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _claims_for(patient_id: str) -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role=Role.PHYSICIAN,
        patient_id=patient_id,
        scopes=_ALL_SCOPES,
        nonce="n",
        jti=f"jti-{patient_id}",
    )


@pytest.fixture
def audit() -> _RecordingAudit:
    return _RecordingAudit()


@pytest.fixture
def registry(audit: _RecordingAudit) -> ToolRegistry:
    return ToolRegistry.from_fixture(
        store=FixtureStore.from_file(),
        audit=audit,
        audit_salt=AUDIT_SALT,
    )


def test_scoped_for_returns_view_bound_to_patient(registry: ToolRegistry) -> None:
    view = registry.scoped_for("101")

    assert isinstance(view, PatientScopedToolRegistry)
    # Frozen dataclass slot — internal field, but the bound value is the
    # whole point of the view, so test the contract directly. A
    # regression that lets the view drop or rebind the id is the kind
    # of bug this test exists to catch.
    assert view._patient_id == "101"


def test_dispatch_uses_bound_patient_id_and_ignores_caller_supplied_input(
    registry: ToolRegistry,
    audit: _RecordingAudit,
) -> None:
    # The orchestrator hands the view a freshly-built ClinicianClaims
    # for patient 101; the view supplies "101" to the underlying tool.
    # The model emitting ``patient_id=999`` in ``tool_use.input``
    # cannot reach this layer — the orchestrator no longer reads that
    # field — but the structural guarantee is that even if it did, the
    # dispatcher signature has no parameter to receive it.
    view = registry.scoped_for("101")

    result = view.dispatch(
        "get_problems",
        claims=_claims_for("101"),
        request_id="req-scoped-happy",
    )

    assert result.tool_name == "get_problems"
    assert result.patient_id == "101"
    # Records load for the bound patient — the source_id pattern pins
    # which patient the tool actually read.
    assert any(record.source_id.startswith("Condition/p101-") for record in result.records)
    # Success audit row written exactly once, against the bound patient.
    assert len(audit.events) == 1
    assert audit.events[0].action == "SUCCESS"
    assert audit.events[0].resource_type == "get_problems"


def test_dispatch_rejects_mismatched_claims_before_tool_runs(
    registry: ToolRegistry,
    audit: _RecordingAudit,
) -> None:
    # The view was bound to 101 but the orchestrator handed it claims
    # for 102. This is a wiring bug — the orchestrator should build
    # the view from the same claims it later dispatches with — and the
    # view fails closed instead of running the tool against either id.
    view = registry.scoped_for("101")

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        view.dispatch(
            "get_problems",
            claims=_claims_for("102"),
            request_id="req-scoped-mismatch",
        )

    # Surfaces the *bound* patient as the requested target — that's
    # what the audit hash should encode if the underlying tool layer
    # ever wrote one. The view itself does not write an audit row
    # because the divergence is a programming error, not an access
    # attempt the audit log needs to capture; the underlying tool's
    # own ``_enforce_rbac`` writes the row only if dispatch reaches it.
    assert excinfo.value.tool_name == "get_problems"
    assert excinfo.value.requested_patient_id == "101"
    assert audit.events == []


def test_anthropic_schemas_strip_patient_id_input(registry: ToolRegistry) -> None:
    # Schemas come from the underlying registry, but the contract is
    # that no tool exposes ``patient_id`` (or any other arg) to the
    # LLM. Re-asserting at the view layer pins the scoped-registry
    # contract independently of the bare-registry test.
    view = registry.scoped_for("101")

    schemas = view.anthropic_schemas()

    assert len(schemas) > 0
    for schema in schemas:
        input_schema = schema["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema["properties"] == {}
        assert input_schema["required"] == []


def test_anthropic_schemas_respect_lane_filter(registry: ToolRegistry) -> None:
    # Lane filtering is delegated to the underlying registry; sanity
    # check that the filter still works through the view so a regression
    # in the delegation doesn't accidentally leak slow-lane tools to
    # the fast lane (where the prompt advertises only a subset).
    view = registry.scoped_for("101")

    fast_lane_subset = frozenset({"get_problems", "get_meds", "get_visits", "get_flags"})
    schemas = view.anthropic_schemas(allowed_names=fast_lane_subset)

    names = {str(schema["name"]) for schema in schemas}
    assert names == fast_lane_subset
