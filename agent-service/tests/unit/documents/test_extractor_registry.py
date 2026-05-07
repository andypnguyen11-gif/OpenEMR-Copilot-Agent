"""Registry-dispatch contract for the document extractor.

Locks two invariants the multimodal expansion (Week 2 Steps 2-7) all
rely on:

  1. Every member of the ``DocumentType`` Literal has a registry entry
     (so a typo in a new ``Literal`` value is caught at test time, not
     at the supervisor's first dispatch).

  2. Stub extractors raise ``UnsupportedDocumentTypeError`` (a subclass
     of ``ExtractorError``) so the eval runner / supervisor can map the
     failure to ``UNSUPPORTED_DOCUMENT_TYPE`` abstention rather than
     conflate it with a transient TOOL_FAILURE.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from clinical_copilot.documents.extractor import (
    DocumentType,
    ExtractorError,
    UnsupportedDocumentTypeError,
    _EXTRACTORS,
    extract,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason


def test_every_document_type_has_a_registry_entry() -> None:
    declared = set(get_args(DocumentType))
    registered = set(_EXTRACTORS.keys())
    assert declared == registered, (
        f"DocumentType / registry drift. "
        f"Declared but unregistered: {declared - registered}. "
        f"Registered but undeclared: {registered - declared}."
    )


@pytest.mark.parametrize(
    "document_type",
    ["referral_docx", "fax_tiff", "workbook_xlsx", "hl7_oru", "hl7_adt"],
)
def test_stub_extractors_raise_unsupported(document_type: str) -> None:
    with pytest.raises(UnsupportedDocumentTypeError) as exc_info:
        extract(
            client=None,  # type: ignore[arg-type]
            model="unused",
            document_id="t",
            document_type=document_type,  # type: ignore[arg-type]
            pdf_path=Path("/tmp/does-not-need-to-exist"),
        )
    # UnsupportedDocumentTypeError is an ExtractorError subclass — both
    # the supervisor and the eval runner branch on this hierarchy.
    assert isinstance(exc_info.value, ExtractorError)


def test_unsupported_abstain_reason_exists() -> None:
    # Documents the contract end-to-end: when an extractor raises
    # UnsupportedDocumentTypeError, the surrounding layer maps to this
    # exact enum member.
    assert RuntimeAbstainReason.UNSUPPORTED_DOCUMENT_TYPE.value == (
        "UNSUPPORTED_DOCUMENT_TYPE"
    )
