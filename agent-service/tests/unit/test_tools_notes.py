"""Unit tests for ``GetNotesFhirTool``.

Coverage in priority order:

* **Base64 decode** — ``Attachment.data`` decodes to UTF-8 and lands as
  the :class:`NoteRecord` body.
* **Author display preferred over reference** — when an author entry
  has both, the human-readable ``display`` wins.
* **Author reference fallback** — display absent, reference used as the
  author string.
* **Unknown author** — neither display nor reference: literal
  ``"Unknown"`` (record-level invariant requires a non-empty string).
* **Drop-on-missing.** Notes without an inline attachment, or with a
  missing date, or with malformed base64, are dropped.
* **ACL denial** — 401/403 → :class:`UnauthorizedToolCallError`.
"""

from __future__ import annotations

import base64

import pytest

from clinical_copilot.audit.log import hash_patient_id
from clinical_copilot.data.fhir_client import FhirError
from clinical_copilot.data.models import (
    Attachment,
    DocumentReference,
    DocumentReferenceContent,
    Reference,
)
from clinical_copilot.runtime.async_bridge import AsyncBridge
from clinical_copilot.tools.base import UnauthorizedToolCallError
from clinical_copilot.tools.notes import GetNotesFhirTool
from clinical_copilot.tools.records import NoteRecord

from ._fhir_tool_helpers import (
    AUDIT_SALT,
    PATIENT_ID,
    RecordingAuditWriter,
    StubFhirClient,
    claims_for,
    expect_record,
)


def _encode(body: str) -> str:
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


def _document(
    *,
    did: str,
    body_text: str | None = "Patient stable on current regimen.",
    authors: tuple[Reference, ...] = (Reference(reference=None, display="Dr. Patel"),),
    date: str | None = "2026-03-14",
    raw_data: str | None = None,
) -> DocumentReference:
    if raw_data is not None:
        data = raw_data
    elif body_text is None:
        data = None
    else:
        data = _encode(body_text)
    content_list = (
        [
            DocumentReferenceContent(
                attachment=Attachment(
                    contentType="text/plain",
                    data=data,
                    url=None,
                    title=None,
                ),
            ),
        ]
        if data is not None
        else []
    )
    return DocumentReference(
        id=did,
        status="current",
        type=None,
        date=date,
        author=list(authors),
        content=content_list,
    )


def test_decodes_base64_body_and_picks_display_author(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(documents=lambda *, patient_id: [_document(did="p101-note-1")])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-1")

    assert len(result.records) == 1
    record = expect_record(result.records[0], NoteRecord)
    assert record.source_id == "DocumentReference/p101-note-1"
    assert record.note_date == "2026-03-14"
    assert record.author == "Dr. Patel"
    assert record.body == "Patient stable on current regimen."


def test_falls_back_to_reference_when_no_display(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    document = _document(
        did="p101-note-2",
        authors=(Reference(reference="Practitioner/dr-patel", display=None),),
    )
    fhir = StubFhirClient(documents=lambda *, patient_id: [document])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-2")

    assert expect_record(result.records[0], NoteRecord).author == "Practitioner/dr-patel"


def test_unknown_author_when_display_and_reference_missing(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    document = _document(
        did="p101-note-3",
        authors=(Reference(reference=None, display=None),),
    )
    fhir = StubFhirClient(documents=lambda *, patient_id: [document])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-3")

    assert expect_record(result.records[0], NoteRecord).author == "Unknown"


def test_drops_document_without_inline_data(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    keepable = _document(did="p101-note-1")
    droppable = _document(did="p101-note-bad", body_text=None)
    fhir = StubFhirClient(documents=lambda *, patient_id: [droppable, keepable])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-4")

    assert len(result.records) == 1
    assert result.records[0].source_id == "DocumentReference/p101-note-1"


def test_drops_document_without_date(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    droppable = _document(did="p101-note-bad", date=None)
    fhir = StubFhirClient(documents=lambda *, patient_id: [droppable])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-5")

    assert result.records == []


def test_drops_document_with_malformed_base64(
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    """Validation matters: invalid base64 must not slip a half-decoded
    bytes string into the record. The projection skips the malformed
    entry rather than failing the whole search.
    """

    droppable = _document(did="p101-note-bad", raw_data="!!!not-base64!!!")
    fhir = StubFhirClient(documents=lambda *, patient_id: [droppable])
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    result = tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-6")

    assert result.records == []


@pytest.mark.parametrize("status_code", [401, 403])
def test_fhir_acl_denial_writes_audit_and_raises(
    status_code: int,
    bridge: AsyncBridge,
    audit: RecordingAuditWriter,
) -> None:
    fhir = StubFhirClient(
        documents=FhirError(f"FHIR client error: status={status_code}", status_code=status_code),
    )
    tool = GetNotesFhirTool(fhir=fhir, bridge=bridge, audit=audit, audit_salt=AUDIT_SALT)

    with pytest.raises(UnauthorizedToolCallError) as excinfo:
        tool.execute(claims=claims_for(), patient_id=PATIENT_ID, request_id="req-acl")

    assert excinfo.value.tool_name == "get_notes"
    assert len(audit.events) == 1
    assert audit.events[0].patient_id_hash == hash_patient_id(PATIENT_ID, salt=AUDIT_SALT)
