"""``evidence_retriever`` LangGraph node — guideline retrieval.

Wraps the v1 :class:`CorpusRetriever` (BM25 + optional dense + RRF +
optional rerank) as a LangGraph node body. The node filters
``state["sub_queries"]`` to those targeting :data:`Worker.EVIDENCE_RETRIEVER`,
runs the existing :func:`run_evidence_retriever` worker once per
sub-query, and emits one :class:`Draft` per result.

The Draft's ``text`` is a small joined preview of the top retrieved
chunks; the citations carry ``corpus_id`` only (chart records are
strictly out of band per A.5). Downstream the critic verifies that
the cited corpus_ids actually back any synthesized claims.

For early-submission scope the node retrieves but does not synthesize
new prose — the synthesizer / verification node downstream stitches
the drafts into the final answer. Keeping the worker prose minimal
keeps the critic's job tractable (it is judging citation kind and
support, not paraphrase quality).
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from anthropic import Anthropic

from clinical_copilot.corpus.retriever import CorpusRetriever
from clinical_copilot.orchestrator.state import (
    Citation,
    Draft,
    SubQuery,
    TurnState,
    Worker,
)
from clinical_copilot.orchestrator.workers.evidence_retriever import (
    EvidenceRetrieverOutput,
    WorkerError,
    run_evidence_retriever,
)
from clinical_copilot.schemas.abstain import RuntimeAbstainReason

logger = structlog.get_logger(__name__)


DEFAULT_K: Final[int] = 5
"""Top-k chunks the worker pulls per sub-query. Matches the W2-06
slow-lane retriever default."""

PREVIEW_CHARS: Final[int] = 240
"""Per-chunk text preview length in the draft prose. Keeps draft
length bounded; the full chunk text is still reachable through the
citation's ``corpus_id``."""


def _output_to_draft(*, sub_query: SubQuery, output: EvidenceRetrieverOutput) -> Draft:
    """Project a worker output into a single :class:`Draft`."""

    if not output.chunks:
        return Draft(
            sub_query_id=sub_query.id,
            worker=Worker.EVIDENCE_RETRIEVER,
            text="",
            citations=(),
            abstain_reason=RuntimeAbstainReason.NO_DATA.value,
        )

    citations = tuple(
        Citation(corpus_id=str(chunk.get("chunk_id") or ""))
        for chunk in output.chunks
        if chunk.get("chunk_id")
    )

    # Join short previews so the critic can match the planner-claim-type
    # sanity checks. The full text is reachable via corpus_id; we never
    # paraphrase here.
    parts = []
    for chunk in output.chunks:
        title = str(chunk.get("title") or chunk.get("source") or "guideline")
        text = str(chunk.get("text") or "")
        preview = text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
        parts.append(f"[{title}] {preview}")

    return Draft(
        sub_query_id=sub_query.id,
        worker=Worker.EVIDENCE_RETRIEVER,
        text="\n".join(parts),
        citations=citations,
    )


def make_node(
    *,
    retriever: CorpusRetriever,
    rerank_client: Anthropic | None = None,
    rerank_model: str | None = None,
    k: int = DEFAULT_K,
) -> Any:
    """Bind retriever / rerank client and return a LangGraph node body.

    The node mutates only ``state["drafts"]`` (additive via the state
    reducer in :mod:`orchestrator.state`). Errors per sub-query become
    :class:`Draft` rows with ``abstain_reason=TOOL_FAILURE`` rather
    than raising — the graph keeps running for sibling sub-queries.
    """

    def node(state: TurnState) -> dict[str, list[Draft]]:
        sub_queries = state.get("sub_queries", [])
        targeted = [sq for sq in sub_queries if sq.target_worker is Worker.EVIDENCE_RETRIEVER]
        session = state.get("session", {})
        request_id = session.get("request_id")
        log = logger.bind(request_id=request_id, count=len(targeted))
        log.info("evidence_retriever.node.invoke")

        drafts: list[Draft] = []
        for sub_query in targeted:
            try:
                output = run_evidence_retriever(
                    retriever=retriever,
                    query=sub_query.text,
                    k=k,
                    rerank_client=rerank_client,
                    rerank_model=rerank_model,
                )
            except WorkerError as exc:
                log.warning(
                    "evidence_retriever.node.worker_error",
                    sub_query_id=sub_query.id,
                    error=str(exc),
                )
                drafts.append(
                    Draft(
                        sub_query_id=sub_query.id,
                        worker=Worker.EVIDENCE_RETRIEVER,
                        text="",
                        citations=(),
                        abstain_reason=RuntimeAbstainReason.TOOL_FAILURE.value,
                    ),
                )
                continue
            drafts.append(_output_to_draft(sub_query=sub_query, output=output))

        return {"drafts": drafts}

    return node
