"""Local CLI for the Week 2 evidence retrieval demo (W2-06 demo cut).

Wraps `corpus.retriever.CorpusRetriever` so a grader can hit the
guideline corpus from the shell::

    uv run python -m clinical_copilot.scripts.retrieve_evidence \\
        --query "USPSTF colorectal cancer screening age"

Prints up to K (default 5) ranked chunks with title, source, URL, and
the chunk text. The full W2-06 retriever runs through LangGraph as a
supervisor tool; this CLI is the demo-time view of the same surface.
"""

from __future__ import annotations

import argparse
import sys

from clinical_copilot.corpus.retriever import CorpusRetriever


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="retrieve_evidence",
        description="Query the Week 2 guideline corpus (BM25-only demo cut).",
    )
    p.add_argument("--query", required=True, help="Free-text clinical question.")
    p.add_argument("--k", type=int, default=5, help="Number of chunks to return.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        retriever = CorpusRetriever()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    results = retriever.retrieve(args.query, k=args.k)
    if not results:
        print("(no results — query had no overlap with the indexed corpus)")
        return 0

    print(f"query: {args.query}")
    print(f"top {len(results)} chunks (BM25):")
    print()
    for rank, hit in enumerate(results, start=1):
        print(f"#{rank}  score={hit.score:.3f}")
        print(f"     {hit.source} — {hit.title}")
        print(f"     {hit.source_url}")
        print(f"     chunk: {hit.chunk_id}")
        snippet = hit.text if len(hit.text) <= 320 else hit.text[:317] + "..."
        for line in snippet.splitlines():
            print(f"     {line}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
