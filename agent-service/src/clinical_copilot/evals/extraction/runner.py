"""``make eval-extraction-gate`` entrypoint.

Runs the 65-case Stage 4A suite end-to-end: load manifest → produce
output per case (live extract, cached replay, or retrieval) → run
rubrics → aggregate → check thresholds + regression vs ``baseline.json``
→ write results JSON + Markdown → exit 0 or non-zero.

CLI flags
=========

* ``--manifest PATH`` — override the default manifest path. Used by
  unit tests with a temp manifest.
* ``--use-cached`` — skip live extraction. Every case must have a
  ``prediction_path`` that exists; otherwise the run fails. Used by
  the integration smoke test (no network, no API key).
* ``--smoke`` — run only cases with ``live_smoke=true`` (a small
  subset that exercises the pipeline live without the full token
  spend).
* ``--write-baseline`` — instead of gating, write the current
  pass-rates to ``baseline.json``. Use after a deliberate quality
  improvement to ratchet up the floor.
* ``--results-dir PATH`` — override results output directory.
* ``--validate-only`` — load cases + labels, do not run rubrics. Used
  by ``make eval-labels-validate`` to catch manifest drift fast.

Live extraction calls the existing ``documents.extractor.extract``
function. The plan deferred the ``ExtractionBackend`` protocol refactor
to the full submission, but the runner already supports a one-line
"load cached prediction from disk" branch — that's the replay path
used by citation-bucket cases that test failure-mode shapes via
hand-crafted bad predictions.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

from clinical_copilot.evals.extraction.cases import (
    EVAL_DATA_ROOT,
    Bucket,
    Case,
    DocumentType,
    RubricCategory,
    load_cases,
)

if TYPE_CHECKING:
    from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.evals.extraction.labels import load_label
from clinical_copilot.evals.extraction.phi import scan_results_file
from clinical_copilot.evals.extraction.results import (
    RunSummary,
    summarize,
    write_json,
    write_markdown,
)
from clinical_copilot.evals.extraction.rubrics import (
    EvalOutput,
    RetrievedChunk,
    RubricOutcome,
    run_rubrics,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

DEFAULT_RESULTS_DIR = EVAL_DATA_ROOT / "results"
DEFAULT_BASELINE_PATH = EVAL_DATA_ROOT / "baseline.json"

DEFAULT_THRESHOLDS: dict[RubricCategory, float] = {
    RubricCategory.SCHEMA_VALID: 1.00,
    RubricCategory.CITATION_PRESENT: 0.95,
    RubricCategory.FACTUALLY_CONSISTENT: 0.90,
    RubricCategory.SAFE_REFUSAL: 1.00,
    RubricCategory.NO_PHI_IN_LOGS: 1.00,
}
DEFAULT_REGRESSION_THRESHOLD_PP = 5.0


@dataclass(frozen=True, slots=True)
class Baseline:
    thresholds: dict[RubricCategory, float]
    last_pass_rates: dict[RubricCategory, float]
    regression_threshold_pp: float

    @classmethod
    def default(cls) -> Baseline:
        return cls(
            thresholds=dict(DEFAULT_THRESHOLDS),
            last_pass_rates={},
            regression_threshold_pp=DEFAULT_REGRESSION_THRESHOLD_PP,
        )


@dataclass(frozen=True, slots=True)
class GateFailure:
    rubric: RubricCategory
    pass_rate: float
    threshold: float | None = None
    last_pass_rate: float | None = None
    note: str = ""


# --------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval-extraction-gate")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--use-cached", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--baseline-path", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Skip exact-count enforcement; only for incremental dev iteration.",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.manifest, allow_partial=args.allow_partial)

    if args.smoke:
        cases = [c for c in cases if c.live_smoke]
        if not cases:
            print("no cases marked live_smoke=true; nothing to do", file=sys.stderr)
            return 0

    if args.use_cached:
        # Filter to cases that do not require a live LLM call:
        # cached predictions (cited prediction_path) plus retrieval-bucket
        # cases (BM25 is local). Useful for offline iteration on rubrics
        # + harness without paying token cost.
        cached_cases = [
            c for c in cases if c.prediction_path is not None or c.bucket is Bucket.RETRIEVAL
        ]
        skipped = len(cases) - len(cached_cases)
        if skipped:
            print(
                f"--use-cached: skipping {skipped} live-only case(s)"
                " (no prediction_path and not retrieval-bucket)",
                file=sys.stderr,
            )
        cases = cached_cases
        if not cases:
            print("no offline cases available; nothing to do", file=sys.stderr)
            return 0

    if args.validate_only:
        for case in cases:
            load_label(case.label_path)  # raises on unreviewed/malformed
        print(f"validated {len(cases)} cases + labels", flush=True)
        return 0

    # Surface the rerank backend in the CI log so a regression
    # against the wrong backend is obvious. Force the lazy load now
    # so any cohere construction error prints before the per-case
    # output starts.
    rerank_backend_label = "cohere" if _get_cohere_client() is not None else "bm25_only"
    print(f"retrieval rerank backend: {rerank_backend_label}", flush=True)

    results: list[tuple[Case, list[RubricOutcome]]] = []
    for case in cases:
        label = load_label(case.label_path)
        output = _produce_output(case, use_cached=args.use_cached)
        outcomes = run_rubrics(case, label, output)
        results.append((case, outcomes))
        _print_case_line(case, outcomes)

    summary = summarize(results)
    json_path = args.results_dir / f"{summary.run_id}.json"
    md_path = args.results_dir / f"{summary.run_id}.md"
    write_json(json_path, summary, results)
    write_markdown(md_path, summary, results)

    # Defense-in-depth: scan the results JSON we just wrote for any PHI
    # sentinel that slipped past the per-case rubric.
    leaks = scan_results_file(json_path)
    if leaks:
        print(f"PHI sentinels detected in {json_path}:", file=sys.stderr)
        for leak in leaks:
            print(f"  - {leak}", file=sys.stderr)
        return 2

    print()
    _print_summary(summary)

    if args.write_baseline:
        _write_baseline(args.baseline_path, summary)
        print(f"\nwrote baseline → {args.baseline_path}")
        return 0

    baseline = _load_baseline(args.baseline_path)
    failures = _check_gate(summary, baseline)
    if failures:
        print("\n!! gate FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {_format_failure(f)}", file=sys.stderr)
        return 1

    print("\n== gate PASSED")
    return 0


# --------------------------------------------------------------- output


def _produce_output(case: Case, *, use_cached: bool) -> EvalOutput:
    """Materialize an :class:`EvalOutput` for the case.

    Order of preference:

    1. If ``use_cached`` is set OR ``case.prediction_path`` exists, load
       the cached prediction JSON (replay path).
    2. For extraction cases with a document_path, call
       ``documents.extractor.extract`` live.
    3. For retrieval cases, call the corpus retriever (Phase 3 wiring).
    4. For refusal cases without a document, build an empty output.
    """

    # 1. Cached / replay path.
    if (use_cached or case.prediction_path is not None) and case.prediction_path:
        return _load_cached_output(case)

    # 2. Live extraction (extraction / citations / missing-data /
    #    refusals that have a document attached).
    if case.document_type in (DocumentType.LAB_PDF, DocumentType.INTAKE_FORM):
        return _live_extract_output(case)

    # 3. Retrieval (Phase 3 wires this up).
    if case.bucket is Bucket.RETRIEVAL and case.query is not None:
        return _retrieve_output(case)

    # 4. Pure refusal case (query without a document, no expected facts).
    return EvalOutput(
        facts=None,
        retrieved=(),
        abstention_reason=RuntimeAbstainReason.NO_DATA.value,
        synthesized_text="",
    )


def _load_cached_output(case: Case) -> EvalOutput:
    """Load a cached prediction JSON. The shape is whatever ``extract``
    would have produced (``facts`` dict + optional retrieved + abstention)."""

    assert case.prediction_path is not None  # narrowed by caller
    payload = json.loads(case.prediction_path.read_text())
    retrieved_raw = payload.get("retrieved") or []
    return EvalOutput(
        facts=payload.get("facts"),
        retrieved=tuple(
            RetrievedChunk(
                source_doc_id=str(c["source_doc_id"]),
                chunk_id=str(c["chunk_id"]),
                score=float(c.get("score", 0.0)),
                text=str(c.get("text", "")),
            )
            for c in retrieved_raw
        ),
        abstention_reason=payload.get("abstention_reason"),
        synthesized_text=str(payload.get("synthesized_text", "")),
        forbidden_phi=tuple(payload.get("forbidden_phi") or ()),
    )


def _live_extract_output(case: Case) -> EvalOutput:
    """Call ``documents.extractor.extract`` live and wrap in EvalOutput."""

    if case.document_path is None:
        return EvalOutput(facts=None)

    # Imported lazily so unit tests / replay-only runs do not pull in
    # the Anthropic SDK or load API credentials at import time.
    from anthropic import Anthropic

    from clinical_copilot.config import get_settings
    from clinical_copilot.documents.extractor import extract

    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; live extraction unavailable. "
            "Use --use-cached for a replay-only run."
        )
    # Bound each attempt at 120s and give the SDK 4 retries (default is 2).
    # CI saw a single APITimeoutError on a vision call tank the whole 65-case
    # gate; tighter per-attempt timeout + more retries keeps total budget
    # similar (4×120s=8m vs 2×600s=20m) but is more resilient to transient
    # network blips on a single page.
    client = Anthropic(api_key=settings.llm_api_key, timeout=120.0, max_retries=4)
    document_type = case.document_type
    assert document_type in (DocumentType.LAB_PDF, DocumentType.INTAKE_FORM)
    runtime_doc_type = "lab_pdf" if document_type is DocumentType.LAB_PDF else "intake_form"
    result = extract(
        client=client,
        model=settings.model_slow,
        document_id=f"eval:{case.case_id}",
        document_type=runtime_doc_type,  # type: ignore[arg-type]
        pdf_path=case.document_path,
    )
    facts = result.facts.model_dump(mode="json")
    abstention = _facts_top_level_abstention(facts)
    return EvalOutput(
        facts=facts,
        abstention_reason=abstention,
    )


def _retrieve_output(case: Case) -> EvalOutput:
    """Call the corpus retriever for a retrieval-bucket case.

    When ``COHERE_API_KEY`` is set, the runner pulls BM25 top-20 and
    reranks via Cohere to top-10 — matching the live chat path's
    rerank quality so the gate measures production retrieval, not
    raw BM25. When the key is absent, falls back to BM25-only top-10
    so dev runs without a Cohere key still execute (with reduced
    accuracy on intent-disambiguation queries like "management of X"
    vs "screening for X").
    """

    if not case.query:
        return EvalOutput(retrieved=())

    # Lazy import — keeps the corpus module out of the import path
    # for offline / cached-only test runs.
    from clinical_copilot.evals.extraction.rubrics import RetrievedChunk

    retriever = _get_corpus_retriever()
    cohere_client = _get_cohere_client()

    if cohere_client is not None:
        # Lazy import so a runner invoked without the rerank module on
        # the path (offline replay tests) doesn't pay the import cost.
        from clinical_copilot.corpus.rerank import rerank_with_cohere

        candidates = retriever.retrieve(query=case.query, k=20)
        chunks = rerank_with_cohere(
            client=cohere_client,
            query=case.query,
            candidates=candidates,
            top_k=10,
        )
    else:
        chunks = retriever.retrieve(query=case.query, k=10)

    return EvalOutput(
        retrieved=tuple(
            RetrievedChunk(
                source_doc_id=c.source_doc_id,
                chunk_id=c.chunk_id,
                score=min(1.0, max(0.0, c.score / 10.0)),  # rough BM25→[0,1]
                text=c.text,
            )
            for c in chunks
        ),
    )


_corpus_retriever_cache: object = None


def _get_corpus_retriever() -> CorpusRetriever:  # type: ignore[name-defined]
    """Build the retriever once per runner invocation."""

    global _corpus_retriever_cache
    if _corpus_retriever_cache is None:
        from clinical_copilot.corpus.retriever import CorpusRetriever

        _corpus_retriever_cache = CorpusRetriever()
    return _corpus_retriever_cache  # type: ignore[return-value]


_cohere_client_cache: Any = None
_cohere_client_loaded: bool = False


def _get_cohere_client() -> Any:
    """Return a cached Cohere ``ClientV2`` or ``None`` when
    ``COHERE_API_KEY`` is absent / construction failed.

    Built once per runner invocation. ``None`` is the documented
    fallback that flips ``_retrieve_output`` to BM25-only — same
    contract as the live chat path's rerank-stage fallback when the
    key isn't wired.
    """

    global _cohere_client_cache, _cohere_client_loaded
    if _cohere_client_loaded:
        return _cohere_client_cache
    _cohere_client_loaded = True

    from clinical_copilot.config import get_settings

    settings = get_settings()
    if not settings.cohere_api_key:
        return None

    try:
        import cohere  # noqa: PLC0415  (lazy import is intentional)

        _cohere_client_cache = cohere.ClientV2(api_key=settings.cohere_api_key)
    except Exception as exc:
        print(
            f"warning: cohere client init failed ({type(exc).__name__}: {exc}); "
            "falling back to BM25-only retrieval",
            file=sys.stderr,
        )
        _cohere_client_cache = None
    return _cohere_client_cache


def _facts_top_level_abstention(facts: dict[str, Any]) -> str | None:
    """If the entire facts payload is a single ``ExtractedField`` whose
    ``abstain_reason`` is set, surface that as the case's abstention.
    Used by missing-data cases that abstain on the chief field."""

    if not isinstance(facts, dict):
        return None
    return facts.get("abstain_reason") if "abstain_reason" in facts else None


# --------------------------------------------------------------- baseline


def _load_baseline(path: Path) -> Baseline:
    if not path.exists():
        return Baseline.default()
    raw = json.loads(path.read_text())
    thresholds = {RubricCategory(k): float(v) for k, v in (raw.get("thresholds") or {}).items()}
    last = {RubricCategory(k): float(v) for k, v in (raw.get("last_pass_rates") or {}).items()}
    return Baseline(
        thresholds={**DEFAULT_THRESHOLDS, **thresholds},
        last_pass_rates=last,
        regression_threshold_pp=float(
            raw.get("regression_threshold_pp", DEFAULT_REGRESSION_THRESHOLD_PP)
        ),
    )


def _write_baseline(path: Path, summary: RunSummary) -> None:
    payload = {
        "thresholds": {k.value: v for k, v in DEFAULT_THRESHOLDS.items()},
        "last_pass_rates": {
            cat.rubric.value: round(cat.pass_rate, 4) for cat in summary.categories
        },
        "regression_threshold_pp": DEFAULT_REGRESSION_THRESHOLD_PP,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _check_gate(summary: RunSummary, baseline: Baseline) -> list[GateFailure]:
    failures: list[GateFailure] = []
    for cat in summary.categories:
        threshold = baseline.thresholds.get(cat.rubric)
        if threshold is not None and cat.pass_rate < threshold:
            failures.append(
                GateFailure(
                    rubric=cat.rubric,
                    pass_rate=cat.pass_rate,
                    threshold=threshold,
                    note="below threshold",
                )
            )
            continue
        last = baseline.last_pass_rates.get(cat.rubric)
        if last is not None:
            drop_pp = (last - cat.pass_rate) * 100.0
            if drop_pp > baseline.regression_threshold_pp:
                failures.append(
                    GateFailure(
                        rubric=cat.rubric,
                        pass_rate=cat.pass_rate,
                        last_pass_rate=last,
                        note=f"regression {drop_pp:.1f}pp",
                    )
                )
    return failures


# --------------------------------------------------------------- printing


def _print_case_line(case: Case, outcomes: list[RubricOutcome]) -> None:
    passed = sum(1 for o in outcomes if o.passed)
    total = len(outcomes)
    flag = "PASS" if passed == total else "FAIL"
    print(f"  · {case.case_id} … {flag} ({passed}/{total})", flush=True)
    for o in outcomes:
        if not o.passed:
            print(f"      - {o.rubric.value}: {o.reason}", flush=True)


def _print_summary(summary: RunSummary) -> None:
    print(f"summary ({summary.case_count} cases):")
    for cat in summary.categories:
        print(f"  {cat.rubric.value:<24}  {cat.passed:>3}/{cat.total:<3}  {cat.pass_rate:.2%}")


def _format_failure(f: GateFailure) -> str:
    if f.threshold is not None and "below" in f.note:
        return f"{f.rubric.value}: pass_rate {f.pass_rate:.2%} < threshold {f.threshold:.2%}"
    if f.last_pass_rate is not None:
        return (
            f"{f.rubric.value}: pass_rate {f.pass_rate:.2%} ({f.note},"
            f" baseline {f.last_pass_rate:.2%})"
        )
    return f"{f.rubric.value}: {f.note}"


if __name__ == "__main__":
    sys.exit(main())
