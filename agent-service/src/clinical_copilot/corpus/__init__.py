"""Guideline corpus + retrieval (PRD2 §7, W2_ARCHITECTURE §6).

Index-time pipeline: read markdown sources from `corpus/sources/`,
parse YAML frontmatter, chunk into sentence-window passages, scrub for
PHI shapes, build a BM25 index, and write a manifest. Query-time
pipeline: load the BM25 index + manifest, run hybrid retrieve (BM25
top-K for the demo cut; pgvector dense + cross-encoder rerank land in
the full W2-06 MR), return ranked chunks with their citations.

The full plan also exposes the retriever as a tool the LangGraph
supervisor can invoke (`tools/guideline_evidence.py`); for tonight the
retriever is reachable via the local CLI in
`scripts/retrieve_evidence.py`.
"""

from __future__ import annotations
