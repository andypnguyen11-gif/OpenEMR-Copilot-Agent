"""``intake_extractor`` worker — wraps the multimodal document extractor.

The supervisor invokes this worker by emitting an Anthropic
``tool_use`` block for ``dispatch_intake_extractor`` with arguments

::

    {
      "document_path": "/abs/or/relative.pdf",
      "document_type": "lab_pdf" | "intake_form"
    }

The worker calls :func:`clinical_copilot.documents.extractor.extract`,
captures the resulting :class:`ExtractionResult`, and returns a JSON-
serializable dict the supervisor can reason over and ultimately surface
to the user.

Contract
========

Inputs are validated up-front. Bad inputs raise :class:`WorkerError`,
which the supervisor converts into a tool_result with ``is_error=True``
so the model can recover or abstain rather than silently see no result.

Outputs always include ``document_id``, ``document_type``, ``facts``,
and ``citations`` (a flattened list of every :class:`SourceCitation`
that landed in the facts). The supervisor uses ``citations`` to
satisfy its synthesis-time "no uncited claim" check.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from anthropic import Anthropic

from clinical_copilot.documents.extractor import (
    DocumentType,
    ExtractionResult,
    ExtractorError,
    extract,
)


class WorkerError(RuntimeError):
    """Raised on invalid input or extractor failure."""


@dataclass(frozen=True, slots=True)
class IntakeExtractorOutput:
    """Structured worker output. Serialized to dict before returning to
    the supervisor."""

    document_id: str
    document_type: str
    facts: dict[str, Any]
    citations: list[dict[str, Any]]
    abstain_reason: str | None = None

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_type": self.document_type,
            "facts": self.facts,
            "citations": self.citations,
            "abstain_reason": self.abstain_reason,
        }


def run_intake_extractor(
    *,
    client: Anthropic,
    model: str,
    document_path: str,
    document_type: str,
    document_id: str | None = None,
) -> IntakeExtractorOutput:
    """Validate inputs, call the extractor, and flatten the result."""

    valid_types = get_args(DocumentType)  # ("lab_pdf", "intake_form")
    if document_type not in valid_types:
        raise WorkerError(f"document_type must be one of {valid_types}, got {document_type!r}")

    pdf_path = Path(document_path)
    if not pdf_path.exists():
        raise WorkerError(f"document_path does not exist: {pdf_path}")
    if not pdf_path.is_file():
        raise WorkerError(f"document_path is not a file: {pdf_path}")

    document_id = document_id or pdf_path.stem
    typed_doc_type: Literal["lab_pdf", "intake_form"] = document_type  # type: ignore[assignment]

    try:
        result: ExtractionResult = extract(
            client=client,
            model=model,
            document_id=document_id,
            document_type=typed_doc_type,
            pdf_path=pdf_path,
        )
    except ExtractorError as exc:
        raise WorkerError(f"extractor failed: {exc}") from exc

    facts_dict = result.facts.model_dump(mode="json")
    citations = _flatten_citations(facts_dict)
    return IntakeExtractorOutput(
        document_id=result.document_id,
        document_type=result.document_type,
        facts=facts_dict,
        citations=citations,
        abstain_reason=None,
    )


def _flatten_citations(facts: object) -> list[dict[str, Any]]:
    """Walk an ``ExtractedField`` tree and collect every citation.

    Duplicate citations (same document_id + page + bbox) are kept as-is
    — the supervisor's synthesis layer is responsible for deduping if
    that matters for its prompt budget.
    """

    out: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            citation = node.get("citation")
            if isinstance(citation, dict):
                out.append(citation)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(facts)
    return out
