"""Typed state contracts for the LangGraph supervisor (W2-07).

The LangGraph graph reads/writes a single :class:`TurnState` dict; nodes
are plain Python that mutate it. Per W2_ARCHITECTURE §4.1 the state
shape is the single source of truth — every node imports its keys from
here, never reaches for raw dict literals.

Why split this off from :mod:`orchestrator.supervisor`:

* `state.py` has no LangGraph / Anthropic dependency, so unit tests for
  edge predicates and the planner's structured-output validator stay
  fast and isolated;
* the plain-Python `tool_use` supervisor (`supervisor.py`) and the
  LangGraph supervisor share the same :class:`SubQuery` / :class:`Draft`
  / :class:`Verdict` vocabulary, so a future merge is straightforward.

The `ClaimType` → `Worker` map (:data:`CLAIM_TYPE_TO_WORKER`) is the
"LLM picks meaning, code picks address" boundary from PRD2 §5.1: the
planner LLM emits a claim type and the routing function reads it back
deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from operator import add
from typing import Annotated, Any, Final, TypedDict

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------- enums


class ClaimType(StrEnum):
    """Per PRD2 §5.1 / Appendix A.5 — what kind of evidence the
    sub-query needs.

    The planner emits one of these for every sub-query; the routing
    map :data:`CLAIM_TYPE_TO_WORKER` then picks the worker. The LLM
    never sees worker names.
    """

    CHART_FACT = "chart_fact"
    DOC_FACT = "doc_fact"
    GUIDELINE = "guideline"


class Worker(StrEnum):
    """Stable worker identifiers used as LangGraph node names."""

    CHART_TOOLS = "chart_tools"
    INTAKE_EXTRACTOR = "intake_extractor"
    EVIDENCE_RETRIEVER = "evidence_retriever"


CLAIM_TYPE_TO_WORKER: Final[Mapping[ClaimType, Worker]] = {
    ClaimType.CHART_FACT: Worker.CHART_TOOLS,
    ClaimType.DOC_FACT: Worker.INTAKE_EXTRACTOR,
    ClaimType.GUIDELINE: Worker.EVIDENCE_RETRIEVER,
}


class RejectionReason(StrEnum):
    """Per PRD2 Appendix A.6 — every critic rejection carries one of
    these. Eval rubrics assert the *expected* rejection reason, so
    silently expanding this taxonomy is a rubric break.
    """

    NO_CITATION = "no_citation"
    CITATION_TYPE_MISMATCH = "citation_type_mismatch"
    ACTION_BLACKLIST = "action_blacklist"
    CONFIDENCE_FLOOR = "confidence_floor"
    JUDGE_REJECTED = "judge_rejected"
    JUDGE_TIMEOUT = "judge_timeout"


class CriticVerdict(StrEnum):
    """Aggregate critic outcome for a single draft."""

    ACCEPT = "accept"
    REJECT = "reject"


# --------------------------------------------------------------- models


class SubQuery(BaseModel):
    """One unit of work the planner emits.

    Frozen so node bodies can pass them around without worrying about
    accidental mutation; node bodies build new instances via
    :meth:`model_copy` when they need to add metadata.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable id; planner assigns a uuid hex.")
    text: str = Field(description="Natural-language sub-query.")
    claim_type: ClaimType = Field(description="Per A.5 routing taxonomy.")
    target_worker: Worker = Field(
        description=(
            "Derived from claim_type via CLAIM_TYPE_TO_WORKER; carried "
            "explicitly so node bodies don't have to re-derive."
        ),
    )


class Citation(BaseModel):
    """Citation a worker draft attaches to a claim.

    Either ``source_id`` (chart record, e.g. ``Observation/123``) or
    ``corpus_id`` (guideline chunk) is non-null, never both. The critic
    enforces the chart-vs-corpus split against the planner-assigned
    claim_type per A.6.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str | None = None
    corpus_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Draft(BaseModel):
    """One worker's response to one sub-query.

    Worker nodes append a :class:`Draft` to ``state["drafts"]`` for
    every sub-query they handle. The critic reads them back and emits
    one :class:`Verdict` per draft.
    """

    model_config = ConfigDict(frozen=True)

    sub_query_id: str
    worker: Worker
    text: str = Field(description="Synthesized prose for this sub-query.")
    citations: tuple[Citation, ...] = Field(default_factory=tuple)
    abstain_reason: str | None = Field(
        default=None,
        description=(
            "If the worker abstained outright (NO_DATA / TOOL_FAILURE / "
            "etc.), populate this and leave ``text`` empty. The critic "
            "passes abstain drafts through unchanged."
        ),
    )


class Verdict(BaseModel):
    """Critic judgment on a single :class:`Draft`."""

    model_config = ConfigDict(frozen=True)

    sub_query_id: str
    verdict: CriticVerdict
    rejection_reason: RejectionReason | None = None
    rationale: str = ""


# --------------------------------------------------------------- state


class SessionInfo(TypedDict, total=False):
    """Light view of the v1 session that the planner reads.

    ``patient_id`` and ``patient_name`` feed the cross-patient guard;
    ``request_id`` is the supervisor-assigned id used for span linkage.
    """

    request_id: str
    patient_id: str
    patient_name: str | None
    history: list[dict[str, Any]]


MAX_RETRIES_PER_SUB_QUERY: Final[int] = 1
"""Per Appendix A.6: at most one retry per sub-query before forcing
abstain on that sub-query.
"""


class TurnState(TypedDict, total=False):
    """LangGraph state shape per W2_ARCHITECTURE §4.1.

    All keys are optional at the type level (``total=False``) so the
    initial state can be constructed with just ``user_query`` and
    ``session``; planner/worker/critic/verification nodes fill in the
    rest as they execute.

    ``drafts`` and ``verdicts`` carry an ``operator.add`` reducer so
    parallel worker fan-out (LangGraph 0.2 returns list values from
    multiple nodes and merges them into the same key) accumulates
    rather than overwrites. The other list-valued keys (``sub_queries``)
    are written by exactly one node so they don't need a reducer.
    """

    user_query: str
    session: SessionInfo
    sub_queries: list[SubQuery]
    drafts: Annotated[list[Draft], add]
    verdicts: Annotated[list[Verdict], add]
    retry_counts: dict[str, int]
    final_response: dict[str, Any] | None
    """Filled by the verification leaf node. Shape mirrors
    :class:`SupervisorResponse` so :func:`main._supervisor_to_agent_response`
    keeps working unchanged.
    """

    rerank_backend: str | None
    """Stamped by the evidence_retriever node when it actually invokes
    a reranker. ``"cohere" | "llm_judge" | "bm25_only"``; ``None`` on
    turns that never reach the evidence retriever (chart-only,
    abstention) so :func:`run_turn` can leave
    :attr:`SupervisorResponse.rerank_backend` ``None`` and the UI badge
    stays off. Single-writer key — only the evidence_retriever node
    mutates it — so no reducer is needed."""


def initial_state(*, user_query: str, session: SessionInfo) -> TurnState:
    """Build a fresh :class:`TurnState` for a single turn.

    Centralized so every entry point (FastAPI route, integration tests,
    eval harness) sees the same default shape — empty lists, empty
    retry counter, no final response yet.
    """

    return TurnState(
        user_query=user_query,
        session=session,
        sub_queries=[],
        drafts=[],
        verdicts=[],
        retry_counts={},
        final_response=None,
        rerank_backend=None,
    )
