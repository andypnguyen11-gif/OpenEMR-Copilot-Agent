"""Local-ingest CLI for the Week 2 demo (replaces W2-02 PHP plumbing).

Production Week 2 ingests a document by:

  1. Uploading it to the Co-Pilot Documents category in OpenEMR.
  2. Symfony EventDispatcher fires `CoPilotDocumentUploadedListener`.
  3. Listener HMAC-signs a payload and POSTs `/internal/documents/enqueue`.
  4. Worker claims the job, runs the extractor, persists `extracted_facts`.

For the local Week 2 demo none of that pipeline ships yet (W2-02). This
CLI bypasses steps 1–3 and runs steps 4–5 directly against a local PDF
path, so a grader can produce the same `extracted_facts` row without
running OpenEMR or the queue.

Usage::

    uv run python -m clinical_copilot.scripts.ingest_document \\
        --pdf tests/fixtures/lab_pdf/glucose_panel.pdf \\
        --type lab_pdf \\
        --document-id lab-001

Pretty-prints the resulting facts and writes
``data/extracted/<document-id>.json``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

from anthropic import Anthropic

from clinical_copilot.config import get_settings
from clinical_copilot.documents import store
from clinical_copilot.documents.extractor import DocumentType, ExtractorError, extract


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest_document",
        description="Run the Week 2 document extractor against a local PDF.",
    )
    p.add_argument("--pdf", required=True, type=Path, help="Path to the source PDF.")
    p.add_argument(
        "--type",
        required=True,
        choices=("lab_pdf", "intake_form"),
        help="Document type. Selects the extraction tool schema.",
    )
    p.add_argument(
        "--document-id",
        required=True,
        help="Stable id for the document (filename or hash works).",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the slow-lane model (defaults to settings.model_slow).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.pdf.exists():
        print(f"error: PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    settings = get_settings()
    if not settings.llm_api_key:
        print(
            "error: ANTHROPIC_API_KEY is not set. "
            "Export it in your shell or in agent-service/.env before running.",
            file=sys.stderr,
        )
        return 2

    client = Anthropic(api_key=settings.llm_api_key)
    model = args.model or settings.model_slow

    print(f"[ingest] document_id={args.document_id} type={args.type} model={model}")
    print(f"[ingest] reading {args.pdf}")

    try:
        result = extract(
            client=client,
            model=model,
            document_id=args.document_id,
            document_type=cast(DocumentType, args.type),
            pdf_path=args.pdf,
        )
    except ExtractorError as exc:
        print(f"error: extraction failed: {exc}", file=sys.stderr)
        return 1

    path = store.write(result.facts)
    print(f"[ingest] wrote {path}")
    print()
    print(result.facts.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
