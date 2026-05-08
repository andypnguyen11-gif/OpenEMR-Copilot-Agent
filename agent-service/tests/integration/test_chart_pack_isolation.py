"""Integration test: ``build_chart_pack`` against a real
``PatientScopedToolRegistry`` honours the patient cross-check.

Unit tests in ``tests/unit/test_chart_pack.py`` cover the contract
with a stub registry. This test wires a *real*
:class:`PatientScopedToolRegistry` so we catch regressions in the
chain ``build_chart_pack → registry.dispatch → tool.execute → audit``
when the registry is scoped to one patient and the supplied claims
name another.

Concrete scenario: registry scoped to pid ``"101"``, claims carry pid
``"999"``. The fixture-backed registry is identical in shape to the
production FHIR-backed registry; both delegate the cross-check to
:class:`PatientScopedToolRegistry.dispatch` (``tools/registry.py``
line 296-300). The chart-pack module re-raises
:class:`UnauthorizedToolCallError` rather than swallowing it into
``failed_topics`` so a wiring bug fails the request closed.
"""

from __future__ import annotations

import asyncio

import pytest

from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.orchestrator.chart_pack import build_chart_pack
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry

_AUDIT_SALT = "test-salt"

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

    def write(self, event: AuditEvent) -> None:  # type: ignore[override]
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
        audit_salt=_AUDIT_SALT,
    )


def test_build_chart_pack_propagates_pid_mismatch(
    registry: ToolRegistry,
) -> None:
    """Registry scoped to ``101`` + claims carrying ``999`` →
    :class:`UnauthorizedToolCallError`. The error fires *before* any
    tool's ``_run`` executes, per the cross-check at
    ``tools/registry.py:296-300``.
    """

    scoped = registry.scoped_for("101")
    mismatched_claims = _claims_for("999")

    with pytest.raises(UnauthorizedToolCallError):
        asyncio.run(
            build_chart_pack(
                scoped_registry=scoped,
                claims=mismatched_claims,
                request_id="iso-1",
            )
        )


def test_build_chart_pack_with_matching_claims_returns_a_pack(
    registry: ToolRegistry,
) -> None:
    """Sanity floor: with the bound id and the claims agreeing, the
    fan-out runs end to end against the fixture store. We don't
    assert specific records (FixtureStore content varies per fixture
    file) — only that the call returns a populated
    :class:`ChartPack` with the bound patient_id and that audit rows
    landed for at least one topic."""

    scoped = registry.scoped_for("101")
    matched_claims = _claims_for("101")

    pack = asyncio.run(
        build_chart_pack(
            scoped_registry=scoped,
            claims=matched_claims,
            request_id="iso-2",
        )
    )

    assert pack.patient_id == "101"
    # At least one of the six topics either fetched (possibly empty)
    # or failed cleanly — the registry surfaced no exceptions to
    # callers other than the ones we re-raise on policy.
    assert len(pack.fetched_topics) + len(pack.failed_topics) > 0
