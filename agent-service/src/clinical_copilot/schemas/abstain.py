"""Canonical abstention enum (PRD2 Appendix A.1.a).

Every place the agent declines to answer maps to exactly one of these
seven members. Three of them — ``LOW_CONFIDENCE``, ``OUT_OF_SCHEMA``,
``CITATION_INVALID`` — are new in Week 2 and arise inside the document
extraction pipeline. The other four are the Week 1 reasons, preserved
verbatim so existing call sites in ``verification``, ``orchestrator``,
and ``observability.metrics`` keep compiling.

The enum is *runtime-only*. The eval harness uses a separate
``EvalCaseState`` for grader-side states; the import-linter contract
forbids either module from importing the other so the surfaces cannot
silently merge.
"""

from __future__ import annotations

from enum import StrEnum


class RuntimeAbstainReason(StrEnum):
    """Why the agent stopped short of a confident answer.

    String-backed: the value is serialized to the wire (response body)
    and into LangSmith trace metadata. Adding a new member is a
    backwards-compatible change; renaming an existing one is not — the
    UI's per-state copy table is keyed on these strings.
    """

    NO_DATA = "NO_DATA"
    """The chart legitimately doesn't contain what the user asked for."""

    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    """The model produced a draft, but at least one cited claim either
    points at a source the agent never fetched or contradicts the field
    value of a source the agent did fetch."""

    TOOL_FAILURE = "TOOL_FAILURE"
    """A tool the orchestrator needed to call raised a non-authorization
    error (timeout, FHIR 5xx, schema-mismatch). Distinct from NO_DATA so
    the UI can offer a retry action."""

    UNAUTHORIZED = "UNAUTHORIZED"
    """The session is not authorized to access the requested resource."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    """Extractor produced a value but per-field confidence is below the
    PRD2 §6 threshold (0.7). Field is dropped from the rendered facts
    and a citation marker is shown in its place."""

    OUT_OF_SCHEMA = "OUT_OF_SCHEMA"
    """VLM emitted a field name or value type the document schema does
    not declare. Implies a programming/model error, not a data gap, so
    it cannot collapse to NO_DATA."""

    CITATION_INVALID = "CITATION_INVALID"
    """The per-field citation failed the OCR check (PRD2 §8.2 /
    Appendix A.4): bbox does not exist on the rendered page, OCR text
    does not match the claimed value, or the bbox is degenerate."""

    UNSUPPORTED_DOCUMENT_TYPE = "UNSUPPORTED_DOCUMENT_TYPE"
    """Caller passed a ``document_type`` the extractor registry does not
    know how to handle. Distinct from TOOL_FAILURE so the UI can surface
    "we don't read that document type yet" instead of a generic retry."""
