"""End-to-end CLI demo for the supervisor + 2 workers.

Walks through a single user query that triggers both workers:

  $ uv run python scripts/demo_supervisor.py \\
        --document tests/fixtures/lab_pdf/glucose_panel.pdf \\
        --document-type lab_pdf \\
        --query "What's notable in this lab and what guidelines apply?"

What it prints, in order:
  1. The user query.
  2. The supervisor's structlog handoff entries (one per worker dispatched).
  3. The synthesized text.
  4. Per-handoff timing + worker output snippet.

This is the demo-video script: open a terminal, run this once, and the
viewer sees the supervisor → worker → synthesis flow without needing
to navigate the deployed UI. The 50-case eval gate run is shown
separately via ``make eval-extraction-gate``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from functools import partial
from pathlib import Path

import structlog
from anthropic import Anthropic

from clinical_copilot.config import get_settings
from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.orchestrator import supervisor
from clinical_copilot.orchestrator.workers.evidence_retriever import (
    run_evidence_retriever,
)
from clinical_copilot.orchestrator.workers.intake_extractor import (
    run_intake_extractor,
)


def _configure_logging() -> None:
    """Pretty-print structlog handoff entries to stderr so the demo
    viewer can see them inline with the rest of the output."""

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_supervisor")
    parser.add_argument(
        "--query",
        default="What does this lab show and what guideline applies?",
        help="The user's question.",
    )
    parser.add_argument(
        "--document",
        type=Path,
        default=None,
        help=(
            "Optional path to a lab PDF or intake form. When set, the "
            "supervisor can dispatch to intake_extractor."
        ),
    )
    parser.add_argument(
        "--document-type",
        choices=["lab_pdf", "intake_form"],
        default="lab_pdf",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="Enable LLM-judge rerank in the evidence_retriever worker.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=4,
    )
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    if not settings.llm_api_key:
        print("error: ANTHROPIC_API_KEY is not set in env / .env.", file=sys.stderr)
        return 2

    client = Anthropic(api_key=settings.llm_api_key)

    # Build the two workers as partial-applied callables; the supervisor
    # invokes them as plain Python functions.
    def intake_callable(**kwargs: object) -> dict:
        document_path = str(kwargs.get("document_path", args.document or ""))
        document_type = str(kwargs.get("document_type", args.document_type))
        document_id = (
            str(kwargs.get("document_id"))
            if kwargs.get("document_id") is not None
            else None
        )
        out = run_intake_extractor(
            client=client,
            model=settings.model_slow,
            document_path=document_path,
            document_type=document_type,
            document_id=document_id,
        )
        return out.to_tool_result()

    retriever = CorpusRetriever()

    def evidence_callable(**kwargs: object) -> dict:
        query = str(kwargs.get("query", ""))
        k = int(kwargs.get("k", 5))
        out = run_evidence_retriever(
            retriever=retriever,
            query=query,
            k=k,
            rerank_client=client if args.rerank else None,
        )
        return out.to_tool_result()

    print(f"\n=== USER QUERY\n{args.query}\n")
    if args.document:
        print(f"=== DOCUMENT\n{args.document} ({args.document_type})\n")

    # If the caller passed --document, hint the supervisor toward the
    # intake_extractor by appending the path to the user content. The
    # tool itself still has to be invoked by the model, but giving it
    # the path explicitly avoids a guessing turn.
    query = args.query
    if args.document:
        query = (
            f"{args.query}\n\n(Document available at "
            f"{args.document!s} — type {args.document_type})"
        )

    response = supervisor.run(
        client=client,
        model=settings.model_slow,
        query=query,
        intake_extractor=intake_callable,
        evidence_retriever=evidence_callable,
        max_iterations=args.max_iterations,
    )

    print("\n=== HANDOFFS")
    for h in response.handoffs:
        print(
            f"  {h.worker:<22} ({h.latency_ms} ms)"
            + (f"  ERROR: {h.error}" if h.error else "")
        )

    print("\n=== SYNTHESIZED RESPONSE\n")
    print(response.synthesized_text or "(no synthesis)")
    if response.abstention_reason:
        print(f"\nabstention: {response.abstention_reason}")

    print(f"\n--- iterations: {response.iterations}")
    return 0 if response.synthesized_text else 1


if __name__ == "__main__":
    sys.exit(main())
