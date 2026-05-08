"""End-to-end CLI smoke for the W2-07 LangGraph supervisor.

Drives :func:`clinical_copilot.orchestrator.supervisor_langgraph.run_turn`
against the local corpus index with a real Anthropic key. When
``LANGSMITH_TRACING=true`` is set, every node invocation auto-emits a
trace to smith.langchain.com — open the project there and you'll see
the planner → workers → synthesizer → critic → verification span tree.

Usage::

    export ANTHROPIC_API_KEY=sk-ant-...
    export LANGSMITH_API_KEY=lsv2_pt_...
    export LANGSMITH_TRACING=true
    export LANGSMITH_PROJECT=copilot-w2-07-local

    uv run python scripts/smoke_langgraph.py \\
        --query "What does USPSTF recommend for type 2 diabetes screening?"

PHI safety: the chart pack is left empty by default (no FHIR call), so
nothing patient-identifying ships to LangSmith from this script. The
synthesized text and retrieved corpus chunks DO ship — that's
guideline content from the public sources we curated, no PHI.

Why this exists separately from ``demo_supervisor.py``:
the older script drives the plain-Python tool_use supervisor
(``orchestrator.supervisor.run``); this one drives the LangGraph
StateGraph (``supervisor_langgraph.run_turn``) so the demo viewer can
see the explicit planner / critic / verification nodes on the trace.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import structlog
from anthropic import Anthropic

from clinical_copilot.config import get_settings
from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.orchestrator.lanes import Lane
from clinical_copilot.orchestrator.supervisor_langgraph import run_turn


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )


def _stub_orchestrator() -> object:
    """Return a stand-in Orchestrator.

    The compiled graph requires an Orchestrator object to register the
    ``v1_single`` node, but for guideline-style queries the planner
    routes to ``fan_out`` and ``v1_single`` is never invoked. The stub
    raises if it's hit by accident — that turns a routing bug into a
    loud failure rather than a silent fallback.
    """

    orch = MagicMock()
    orch.run.side_effect = AssertionError(
        "v1_single ran during smoke — your query routed to the §4.5 "
        "short-circuit (single CHART_FACT). Pick a guideline question "
        "instead, or wire a real Orchestrator.",
    )
    return orch


def _stub_claims(patient_id: str) -> object:
    claims = MagicMock()
    claims.patient_id = patient_id
    return claims


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_langgraph")
    parser.add_argument(
        "--query",
        default="What does USPSTF recommend for type 2 diabetes screening?",
        help=(
            "Guideline-style question. Avoid 'what's her A1c' style — "
            "those route to v1_single (chart-fact short-circuit) which "
            "the smoke script stubs out."
        ),
    )
    parser.add_argument(
        "--patient-id",
        default="smoke-patient-001",
        help="Synthetic patient id for the request session info.",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="Enable the LLM-judge rerank stage in evidence_retriever.",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    if not settings.llm_api_key:
        print("error: ANTHROPIC_API_KEY is not set in env / .env.", file=sys.stderr)
        return 2

    client = Anthropic(api_key=settings.llm_api_key)

    # Local corpus index — bm25.pkl is required, dense.pkl is optional
    # (gated on OPENAI_API_KEY at index-build time).
    try:
        retriever = CorpusRetriever()
    except FileNotFoundError as exc:
        print(f"error: corpus index missing — {exc}", file=sys.stderr)
        print(
            "build it with `make corpus-build` (or whichever script "
            "your repo uses) before running this smoke.",
            file=sys.stderr,
        )
        return 3

    print(f"\n=== USER QUERY\n{args.query}\n")
    print(f"=== CORPUS  bm25={Path('data/corpus/bm25.pkl').exists()}, "
          f"hybrid={retriever.hybrid_enabled}\n")
    print("=== LANGSMITH")
    import os as _os  # noqa: PLC0415 — lazy import, only used for the banner
    if _os.environ.get("LANGSMITH_TRACING", "").strip().lower() in {"true", "1", "yes", "on"}:
        project = _os.environ.get("LANGSMITH_PROJECT") or "default"
        print(f"  tracing=on  project={project!r}")
        print("  open smith.langchain.com after the run to see the trace tree.\n")
    else:
        print("  tracing=off  (set LANGSMITH_TRACING=true to record spans)\n")

    response = run_turn(
        user_query=args.query,
        request_id="smoke-langgraph-001",
        patient_id=args.patient_id,
        bound_patient_name=None,
        planner_client=client,
        planner_model=settings.model_fast,
        synthesizer_client=client,
        synthesizer_model=settings.model_slow,
        critic_client=client,
        critic_model=settings.model_fast,
        retriever=retriever,
        rerank_client=client if args.rerank else None,
        rerank_model=settings.model_fast if args.rerank else None,
        orchestrator=_stub_orchestrator(),  # type: ignore[arg-type]
        claims=_stub_claims(args.patient_id),  # type: ignore[arg-type]
        session_id=None,
        lane=Lane.SLOW,
        chart_pack=None,
    )

    print("\n=== SYNTHESIZED RESPONSE\n")
    print(response.synthesized_text or "(no synthesis)")
    if response.abstention_reason:
        print(f"\nabstention: {response.abstention_reason}")

    print(f"\n--- iterations: {response.iterations}")
    return 0 if response.synthesized_text else 1


if __name__ == "__main__":
    raise SystemExit(main())
