"""Critic node — judges each :class:`Draft` against its citations.

Two-tier per W2_ARCHITECTURE §4.4:

1. **Deterministic checks** (no LLM call). Catch the rejection cases
   that a fast string scan can prove cheaply: missing citations,
   chart-vs-corpus mismatch, action-suggestion verbs, low confidence.
2. **LLM judge** runs only on drafts that pass (1). Bounded by a
   1.5 s timeout (PRD2 §10.1, A.6). On timeout the draft is rejected
   with :data:`RejectionReason.JUDGE_TIMEOUT` so the route function
   can decide retry vs. abstain.

The critic is per-draft. A turn with three drafts produces three
verdicts. The router (:func:`route_after_critic`) aggregates.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from importlib import resources
from typing import Any, Final, cast

import structlog
from anthropic import Anthropic
from anthropic.types import Message, MessageParam, ToolParam, ToolUseBlock
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clinical_copilot.orchestrator.state import (
    ClaimType,
    CriticVerdict,
    Draft,
    RejectionReason,
    SubQuery,
    TurnState,
    Verdict,
)

logger = structlog.get_logger(__name__)


# Action-suggestion verb blacklist per Appendix A.6. The regex is
# anchored on word boundaries to avoid matching "starter" or
# "discontinuation"; the suffixed variants ("recommend X-ing") are
# caught by the bare verb plus the trailing -ing pattern.
_ACTION_VERBS: Final[tuple[str, ...]] = (
    "start",
    "stop",
    "increase",
    "decrease",
    "switch to",
    "discontinue",
)
_ACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in _ACTION_VERBS) + r")\b",
    re.IGNORECASE,
)
_RECOMMEND_VERB_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\brecommend(?:ing|ed|s)?\s+\w+ing\b",
    re.IGNORECASE,
)

CONFIDENCE_FLOOR: Final[float] = 0.7
JUDGE_TIMEOUT_SECONDS: Final[float] = 1.5
JUDGE_TOOL_NAME: Final[str] = "emit_verdict"
DEFAULT_MAX_TOKENS: Final[int] = 256


# --------------------------------------------------------------- judge output


class JudgeOutput(BaseModel):
    """Structured output the LLM judge emits via ``emit_verdict``."""

    model_config = ConfigDict(frozen=True)

    verdict: CriticVerdict
    rejection_reason: RejectionReason | None = None
    rationale: str = Field(default="", max_length=400)


# --------------------------------------------------------------- prompt + tool schema


def _system_prompt() -> str:
    return (
        resources.files("clinical_copilot.orchestrator.prompts")
        .joinpath("critic.txt")
        .read_text(encoding="utf-8")
    )


def _tool_schema() -> ToolParam:
    return cast(
        ToolParam,
        {
            "name": JUDGE_TOOL_NAME,
            "description": "Emit ACCEPT or REJECT for the drafted answer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": [v.value for v in CriticVerdict],
                    },
                    "rejection_reason": {
                        "type": "string",
                        "enum": [r.value for r in RejectionReason],
                    },
                    "rationale": {
                        "type": "string",
                        "maxLength": 400,
                    },
                },
                "required": ["verdict"],
            },
        },
    )


# --------------------------------------------------------------- deterministic checks


def _has_action_suggestion(text: str) -> bool:
    """True if the prose contains one of the blacklisted action verbs."""

    return bool(_ACTION_PATTERN.search(text) or _RECOMMEND_VERB_PATTERN.search(text))


def _citation_type_matches(*, claim_type: ClaimType, draft: Draft) -> bool:
    """True iff the chart-vs-corpus citation kind matches the planner
    claim type. Doc-fact claims accept either side because intake-form
    extracted facts may carry both shapes during reconciliation work
    (W2-08 territory; treated permissively for now).
    """

    if not draft.citations:
        return False
    if claim_type is ClaimType.CHART_FACT:
        return any(c.source_id is not None and c.corpus_id is None for c in draft.citations)
    if claim_type is ClaimType.GUIDELINE:
        return any(c.corpus_id is not None and c.source_id is None for c in draft.citations)
    # ClaimType.DOC_FACT — either side is acceptable.
    return any(c.source_id is not None or c.corpus_id is not None for c in draft.citations)


def _confidence_floor_passes(draft: Draft) -> bool:
    """All non-null citation confidences must clear :data:`CONFIDENCE_FLOOR`.

    Confidences default to ``None`` for chart records (we don't ask the
    chart tool to invent a number), so a ``None`` confidence is treated
    as "not applicable" — pass.
    """

    return all(
        c.confidence is None or c.confidence >= CONFIDENCE_FLOOR
        for c in draft.citations
    )


def deterministic_check(*, draft: Draft, sub_query: SubQuery) -> Verdict | None:
    """Run the fast checks. Returns a REJECT :class:`Verdict` on the
    first failure, or ``None`` if all checks pass (so the LLM judge
    runs next).

    Order matters. We fail fastest checks first so an obviously bad
    draft never reaches the LLM judge — the latency win is the whole
    point of the two-tier split.
    """

    if draft.abstain_reason is not None:
        # Worker already abstained — pass through unchanged. The
        # verification node converts abstain reasons to UI text.
        return Verdict(sub_query_id=draft.sub_query_id, verdict=CriticVerdict.ACCEPT)

    if not draft.citations:
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.NO_CITATION,
            rationale="Draft has no citations.",
        )

    if not _citation_type_matches(claim_type=sub_query.claim_type, draft=draft):
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.CITATION_TYPE_MISMATCH,
            rationale=(
                f"claim_type={sub_query.claim_type.value} but citation kind"
                " does not match (chart vs. corpus split per A.5)."
            ),
        )

    if _has_action_suggestion(draft.text):
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.ACTION_BLACKLIST,
            rationale="Draft contains a clinical action verb (start/stop/etc.).",
        )

    if not _confidence_floor_passes(draft):
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.CONFIDENCE_FLOOR,
            rationale=f"Citation confidence below {CONFIDENCE_FLOOR}.",
        )

    return None


# --------------------------------------------------------------- LLM judge


def _format_judge_user_message(*, draft: Draft, sub_query: SubQuery) -> str:
    citations_payload = [c.model_dump(exclude_none=True) for c in draft.citations]
    return (
        f"sub_query: {sub_query.text}\n"
        f"claim_type: {sub_query.claim_type.value}\n"
        f"draft_prose: {draft.text}\n"
        f"citations: {json.dumps(citations_payload)}\n"
    )


def _run_judge(
    *,
    client: Anthropic,
    model: str,
    draft: Draft,
    sub_query: SubQuery,
    max_tokens: int,
) -> Verdict:
    messages: list[MessageParam] = [
        {"role": "user", "content": _format_judge_user_message(draft=draft, sub_query=sub_query)},
    ]
    response: Message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_system_prompt(),
        tools=[_tool_schema()],
        tool_choice={"type": "tool", "name": JUDGE_TOOL_NAME},
        messages=messages,
    )

    tool_use = next(
        (b for b in response.content if isinstance(b, ToolUseBlock)),
        None,
    )
    if tool_use is None:
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.JUDGE_REJECTED,
            rationale="judge returned no tool_use",
        )

    raw = tool_use.input
    if isinstance(raw, str | bytes | bytearray):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}

    try:
        parsed = JudgeOutput.model_validate(raw)
    except ValidationError as exc:
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.REJECT,
            rejection_reason=RejectionReason.JUDGE_REJECTED,
            rationale=f"judge output invalid: {exc.error_count()} errors",
        )

    if parsed.verdict is CriticVerdict.ACCEPT:
        return Verdict(
            sub_query_id=draft.sub_query_id,
            verdict=CriticVerdict.ACCEPT,
            rationale=parsed.rationale,
        )

    return Verdict(
        sub_query_id=draft.sub_query_id,
        verdict=CriticVerdict.REJECT,
        rejection_reason=parsed.rejection_reason or RejectionReason.JUDGE_REJECTED,
        rationale=parsed.rationale,
    )


# --------------------------------------------------------------- public API


def judge(
    *,
    client: Anthropic,
    model: str,
    draft: Draft,
    sub_query: SubQuery,
    timeout_seconds: float = JUDGE_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    request_id: str | None = None,
) -> Verdict:
    """Judge one draft. Deterministic checks first; LLM judge with
    timeout if those pass.

    Exposed at module level so unit tests can call it directly with a
    mock Anthropic client. The LangGraph wrapper :func:`make_node`
    iterates this over ``state["drafts"]``.
    """

    log = logger.bind(
        request_id=request_id,
        sub_query_id=draft.sub_query_id,
        claim_type=sub_query.claim_type.value,
    )

    deterministic = deterministic_check(draft=draft, sub_query=sub_query)
    if deterministic is not None:
        log.info(
            "critic.deterministic",
            verdict=deterministic.verdict.value,
            rejection_reason=(
                deterministic.rejection_reason.value
                if deterministic.rejection_reason
                else None
            ),
        )
        return deterministic

    # LLM judge in a worker thread so we can enforce the timeout
    # without contaminating the call site with asyncio plumbing —
    # node bodies are sync per LangGraph's plain-Python contract.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _run_judge,
            client=client,
            model=model,
            draft=draft,
            sub_query=sub_query,
            max_tokens=max_tokens,
        )
        try:
            verdict = future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            log.warning("critic.judge_timeout", timeout_seconds=timeout_seconds)
            return Verdict(
                sub_query_id=draft.sub_query_id,
                verdict=CriticVerdict.REJECT,
                rejection_reason=RejectionReason.JUDGE_TIMEOUT,
                rationale=f"judge exceeded {timeout_seconds}s",
            )

    log.info(
        "critic.judge",
        verdict=verdict.verdict.value,
        rejection_reason=(
            verdict.rejection_reason.value if verdict.rejection_reason else None
        ),
    )
    return verdict


def make_node(
    *,
    client: Anthropic,
    model: str,
) -> Any:
    """Bind the critic to an Anthropic client / model and return the
    LangGraph node body. The node iterates ``state["drafts"]`` and
    appends one :class:`Verdict` per draft to ``state["verdicts"]``.

    Sub-queries that have no draft (worker never wrote one) are not
    judged here — :func:`route_after_critic` treats that as exhausted-
    abstain via the verdicts list, and verification surfaces the
    NO_DATA. This keeps the critic body purely about evidence-vs-claim
    judgment, not about absent drafts.
    """

    def node(state: TurnState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        sub_queries = state.get("sub_queries", [])
        sq_by_id = {sq.id: sq for sq in sub_queries}
        session = state.get("session", {})
        request_id = session.get("request_id")

        verdicts: list[Verdict] = []
        for draft in drafts:
            sub_query = sq_by_id.get(draft.sub_query_id)
            if sub_query is None:
                # A draft for an unknown sub_query_id is a wiring bug.
                # Reject loudly so the issue surfaces in eval rather
                # than silently rendering.
                verdicts.append(
                    Verdict(
                        sub_query_id=draft.sub_query_id,
                        verdict=CriticVerdict.REJECT,
                        rejection_reason=RejectionReason.JUDGE_REJECTED,
                        rationale="orphan draft: no matching sub_query",
                    ),
                )
                continue
            verdicts.append(
                judge(
                    client=client,
                    model=model,
                    draft=draft,
                    sub_query=sub_query,
                    request_id=request_id,
                ),
            )
        return {"verdicts": verdicts}

    return node
