"""Unit tests for ``GetLabsFhirTool``.

Coverage in priority order:

* **Quantity → numeric string.** Integers render without a decimal
  point ("7" not "7.0"); floats render via ``%g`` so OpenEMR's "0.9"
  doesn't drift to "0.8999...".
* **String result.** ``valueString`` (e.g. "Negative") projects
  unchanged with ``unit=None``.
* **CodeableConcept result.** Coded qualitative results (e.g. coded
  "positive") project via ``preferred_display`` with ``unit=None``.
* **Drop-on-missing.** Observations missing a code/display, value, or
  ``effectiveDateTime`` are dropped — none of the three can be
  fabricated for a citation.
* **Reference range.** ``text`` wins over ``low``/``high``; range with
  only one bound formats as ``>=N`` or ``<=N``.
* **ACL denial** — 401/403 → :class:`UnauthorizedToolCallError`.
"""

from __future__ import annotations

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    CodeableConcept,
    Coding,
    Observation,
    ObservationReferenceRange,
    Quantity,
)
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.labs import GetLabsFhirTool
from clinical_copilot.tools.records import LabRecord

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)

_DEFAULT_QUANTITY: Quantity | None = Quantity(value=7.1, unit="%")


def _obs(
    *,
    oid: str,
    code: str = "4548-4",
    display: str = "Hemoglobin A1c",
    value_quantity: Quantity | None = _DEFAULT_QUANTITY,
    value_string: str | None = None,
    value_coded: CodeableConcept | None = None,
    effective: str | None = "2026-03-14",
    ranges: tuple[ObservationReferenceRange, ...] = (),
) -> Observation:
    return Observation(
        id=oid,
        status="final",
        code=CodeableConcept(coding=[Coding(code=code, display=display)]),
        effectiveDateTime=effective,
        valueQuantity=value_quantity,
        valueString=value_string,
        valueCodeableConcept=value_coded,
        referenceRange=list(ranges),
    )


def test_projects_quantity_with_unit_to_lab_record(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(labs=lambda *, patient_id: [_obs(oid="p101-lab-1")])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert len(result.records) == 1
    record = expect_record(result.records[0], LabRecord)
    assert record.source_id == "Observation/p101-lab-1"
    assert record.code == "4548-4"
    assert record.display == "Hemoglobin A1c"
    assert record.value == "7.1"
    assert record.unit == "%"
    assert record.observed_on == "2026-03-14"


def test_renders_integer_quantity_without_decimal(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(oid="p101-lab-2", value_quantity=Quantity(value=7.0, unit="mg/dL"))
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], LabRecord).value == "7"


def test_projects_value_string_with_no_unit(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(
        oid="p101-lab-3",
        value_quantity=None,
        value_string="Negative",
    )
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    record = expect_record(result.records[0], LabRecord)
    assert record.value == "Negative"
    assert record.unit is None


def test_projects_coded_value(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(
        oid="p101-lab-4",
        value_quantity=None,
        value_coded=CodeableConcept(text="Reactive"),
    )
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    record = expect_record(result.records[0], LabRecord)
    assert record.value == "Reactive"
    assert record.unit is None


def test_drops_observation_without_value(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = _obs(oid="p101-lab-bad", value_quantity=None)
    keepable = _obs(oid="p101-lab-1")
    fhir = StubFhirClient(labs=lambda *, patient_id: [droppable, keepable])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-5")

    assert len(result.records) == 1
    assert result.records[0].source_id == "Observation/p101-lab-1"


def test_drops_observation_without_effective_datetime(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = _obs(oid="p101-lab-bad", effective=None)
    fhir = StubFhirClient(labs=lambda *, patient_id: [droppable])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-6")

    assert result.records == []


def test_reference_range_text_wins(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(
        oid="p101-lab-1",
        ranges=(
            ObservationReferenceRange(
                low=Quantity(value=4.0, unit="%"),
                high=Quantity(value=5.6, unit="%"),
                text="<5.7",
            ),
        ),
    )
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-7")

    assert expect_record(result.records[0], LabRecord).reference_range == "<5.7"


def test_reference_range_low_high_when_no_text(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(
        oid="p101-lab-1",
        ranges=(
            ObservationReferenceRange(
                low=Quantity(value=0.6, unit="mg/dL"),
                high=Quantity(value=1.2, unit="mg/dL"),
                text=None,
            ),
        ),
    )
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-8")

    assert expect_record(result.records[0], LabRecord).reference_range == "0.6-1.2"


def test_reference_range_single_bound(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    obs = _obs(
        oid="p101-lab-1",
        ranges=(
            ObservationReferenceRange(
                low=Quantity(value=10.0, unit="mg/dL"),
                high=None,
                text=None,
            ),
        ),
    )
    fhir = StubFhirClient(labs=lambda *, patient_id: [obs])
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-9")

    assert expect_record(result.records[0], LabRecord).reference_range == ">=10"


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_writes_audit_and_raises(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        labs=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetLabsFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_labs"
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)
