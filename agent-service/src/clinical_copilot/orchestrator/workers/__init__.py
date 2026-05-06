"""Supervisor-callable workers.

Two workers, named per Week 2 PRD:

* :mod:`intake_extractor` — multimodal extractor for lab PDFs and
  intake forms. Wraps :func:`clinical_copilot.documents.extractor.extract`.
* :mod:`evidence_retriever` — hybrid RAG over the guideline corpus.
  Wraps :class:`clinical_copilot.corpus.retriever.CorpusRetriever`.

Each worker is a thin adapter that converts the supervisor's
JSON-serializable tool inputs into the runtime call, and the runtime
result back into a JSON-serializable dict the supervisor can hand
to its synthesis step.

Workers do not call out to other workers, do not write to logs other
than via structlog, and do not depend on each other. The supervisor is
the only thing that knows about the worker set.
"""

from __future__ import annotations
