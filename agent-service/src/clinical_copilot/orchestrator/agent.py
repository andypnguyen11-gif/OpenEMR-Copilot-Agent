"""Single-loop tool-using orchestrator.

ARCHITECTURE §1.2. The flow per request is:

1. Resolve the conversation session via :class:`SessionStore` — restore
   prior ``messages`` and ``tool_results`` so multi-turn continuity
   works. The store also acquires a per-key lock for the duration of
   this run, dropped on update or release in the ``try / finally``.
2. Send the user query + system prompt + tool defs to the LLM.
3. While the LLM emits ``tool_use`` blocks, dispatch each through
   :class:`ToolRegistry` (which performs the per-tool RBAC check) and
   feed the typed results back as ``tool_result`` blocks.
4. When the LLM emits a final text turn, parse it as
   :class:`ModelDraft`. One schema-violation retry; a second failure
   becomes a ``VERIFICATION_FAILED`` whole-response abstention.
5. Run the draft through :class:`VerificationMiddleware`. The middleware
   either passes the draft through or replaces it with an abstention.

Failure modes mapped to abstention states:

* RBAC denial from any tool → ``UNAUTHORIZED`` (audit row already
  written by the tool layer — see ``Tool._enforce_rbac``).
* Tool raised any other error → ``TOOL_FAILURE``.
* Loop exceeded ``max_turns`` → ``TOOL_FAILURE`` ("agent could not
  converge"); shielding against runaway tool loops.
* Final JSON failed schema validation twice → ``VERIFICATION_FAILED``.

**Persisted-vs-working messages.** The loop maintains two parallel
lists. ``persisted_messages`` is the canonical conversation record that
turn N+1 will inherit from the store. ``working_messages`` is what we
hand to the LLM on each call — same as ``persisted_messages`` until a
schema-violation retry, where the corrective frames are appended to
``working_messages`` only. Tool-use rounds (assistant tool_use +
matching tool_result) are legitimate conversation turns and append to
both. Without this split, retry traffic would pollute the next turn's
context (the model would see its own bad JSON and the corrective
prompt). On any abstention path we persist the prior state unchanged —
the abstention itself is server synthesis, not part of the chat
record.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from clinical_copilot.logging import get_logger
from clinical_copilot.observability import traceable_orchestrator_run
from clinical_copilot.orchestrator.llm_gateway import LlmGateway, LlmTurn
from clinical_copilot.orchestrator.schemas import AgentResponse, ModelDraft
from clinical_copilot.orchestrator.sessions import SessionState, SessionStore
from clinical_copilot.tools.base import (
    ToolError,
    UnauthorizedToolCallError,
)
from clinical_copilot.tools.records import ToolResult
from clinical_copilot.verification.abstention import Abstention, AbstentionState
from clinical_copilot.verification.middleware import VerificationMiddleware

if TYPE_CHECKING:
    from clinical_copilot.auth.session import ClinicianClaims
    from clinical_copilot.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_slow.md"
DEFAULT_MAX_TURNS = 8

_LOG = get_logger(__name__)


def _validation_error_trace(exc: ValidationError) -> list[dict[str, object]]:
    """Structured validation metadata safe for logs — no ``input`` values."""

    out: list[dict[str, object]] = []
    for item in exc.errors():
        loc = item.get("loc")
        loc_serializable: object
        if isinstance(loc, tuple):
            loc_serializable = [str(part) for part in loc]
        else:
            loc_serializable = loc if loc is not None else []
        out.append(
            {
                "loc": loc_serializable,
                "type": item.get("type", "unknown"),
            },
        )
    return out


class Orchestrator:
    """One orchestrator per app, configured at startup."""

    def __init__(
        self,
        *,
        llm: LlmGateway,
        registry: ToolRegistry,
        verifier: VerificationMiddleware,
        sessions: SessionStore,
        system_prompt: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._verifier = verifier
        self._sessions = sessions
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT_PATH.read_text(
            encoding="utf-8"
        )
        self._max_turns = max_turns

    @traceable_orchestrator_run
    def run(
        self,
        *,
        query: str,
        claims: ClinicianClaims,
        request_id: str,
        session_id: str | None = None,
    ) -> AgentResponse:
        canonical_id, prior_state = self._sessions.get_or_create(claims, session_id)
        try:
            response, persisted_messages, persisted_tool_results = self._execute(
                query=query,
                claims=claims,
                request_id=request_id,
                prior_state=prior_state,
            )
            self._sessions.update(
                claims,
                canonical_id,
                SessionState(
                    messages=persisted_messages,
                    tool_results=persisted_tool_results,
                ),
            )
            return response.model_copy(update={"session_id": canonical_id})
        except BaseException:
            # Drop the per-key lock without persisting. The prior state
            # remains in the store unchanged — the next turn restores
            # it cleanly.
            self._sessions.release(claims, canonical_id)
            raise

    def _execute(
        self,
        *,
        query: str,
        claims: ClinicianClaims,
        request_id: str,
        prior_state: SessionState,
    ) -> tuple[AgentResponse, list[dict[str, Any]], list[ToolResult]]:
        """Run one user turn against the LLM tool-use loop.

        Returns ``(response, persisted_messages, persisted_tool_results)``.
        On success: ``persisted_messages`` includes the new user turn
        plus every tool-use round plus the final assistant text turn,
        and ``persisted_tool_results`` includes every tool result accumulated
        in this turn.
        On any abstention path: both lists are the prior state unchanged
        — abstentions are server synthesis, not part of the chat record.
        """

        # The static system prompt declares "the session is bound to one
        # patient_id" but doesn't carry the value. Append a per-request
        # session block so the model knows which patient to scope tool
        # calls to. The tool layer still enforces this at call time —
        # this just stops the model from asking the user.
        runtime_system = (
            self._system_prompt
            + "\n\n## Session\n"
            + f"- patient_id: {claims.patient_id}\n"
            + f"- clinician role: {claims.role}\n"
        )

        # ``persisted_messages`` is what turn N+1 will inherit from the
        # store. ``working_messages`` is what we hand to the LLM each
        # call. They diverge only on a schema-violation retry, where
        # corrective frames must NOT enter session history. See module
        # docstring "Persisted-vs-working messages".
        new_user_turn: dict[str, Any] = {"role": "user", "content": query}
        persisted_messages: list[dict[str, Any]] = [*prior_state.messages, new_user_turn]
        working_messages: list[dict[str, Any]] = list(persisted_messages)
        tool_results: list[ToolResult] = list(prior_state.tool_results)
        retried = False

        for _ in range(self._max_turns):
            turn = self._llm.complete(
                system=runtime_system,
                tools=self._registry.anthropic_schemas(),
                messages=working_messages,
            )

            if turn.tool_uses:
                tool_messages, abstention = self._dispatch_tools(
                    turn=turn,
                    claims=claims,
                    request_id=request_id,
                    tool_results=tool_results,
                )
                if abstention is not None:
                    response = AgentResponse(
                        cards=[],
                        prose=[],
                        tool_results=tool_results,
                        abstention=abstention,
                    )
                    return response, list(prior_state.messages), list(prior_state.tool_results)
                # Legitimate tool-use round — both message lists track it.
                persisted_messages.extend(tool_messages)
                working_messages.extend(tool_messages)
                continue

            text = turn.text.strip()
            try:
                draft = ModelDraft.model_validate_json(text)
            except ValidationError as exc:
                _LOG.warning(
                    "orchestrator.model_draft_schema_validation_failed",
                    request_id=request_id,
                    validation_errors=_validation_error_trace(exc),
                )
                if retried:
                    response = AgentResponse(
                        cards=[],
                        prose=[],
                        tool_results=tool_results,
                        abstention=Abstention(
                            state=AbstentionState.VERIFICATION_FAILED,
                            reason="model emitted JSON that failed schema validation twice",
                        ),
                    )
                    return response, list(prior_state.messages), list(prior_state.tool_results)
                retried = True
                # Retry frames go to working_messages only — they must
                # not pollute turn N+1's restored context. Removing
                # this split would re-feed the model its own bad JSON
                # and the corrective prompt on every subsequent turn.
                working_messages.append({"role": "assistant", "content": turn.raw_assistant_blocks})
                working_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous turn did not match the required JSON schema. "
                            f"Validation error: {exc}. Re-emit the response as a single "
                            "JSON object matching the schema in the system prompt."
                        ),
                    }
                )
                continue

            verified = self._verifier.verify(draft=draft, tool_results=tool_results)
            if verified.abstention is not None:
                # Verifier rejected — same rollback contract as a tool
                # abstention. The model's draft is unreliable; nothing
                # about this turn belongs in the session record.
                return verified, list(prior_state.messages), list(prior_state.tool_results)

            # Success path: commit the final assistant text turn into
            # persisted_messages so turn N+1 sees it.
            persisted_messages.append({"role": "assistant", "content": turn.raw_assistant_blocks})
            return verified, persisted_messages, tool_results

        max_turns_response = AgentResponse(
            cards=[],
            prose=[],
            tool_results=tool_results,
            abstention=Abstention(
                state=AbstentionState.TOOL_FAILURE,
                reason=f"agent did not converge within {self._max_turns} turns",
            ),
        )
        return max_turns_response, list(prior_state.messages), list(prior_state.tool_results)

    def _dispatch_tools(
        self,
        *,
        turn: LlmTurn,
        claims: ClinicianClaims,
        request_id: str,
        tool_results: list[ToolResult],
    ) -> tuple[list[dict[str, Any]], Abstention | None]:
        """Run every tool the model called this turn, build the next
        user message containing matching ``tool_result`` blocks, and
        return either a continuation or a short-circuit abstention.

        On the first RBAC denial we surface ``UNAUTHORIZED`` and stop —
        a session that's tried to escape its scope cannot recover by
        retrying. Other tool errors collapse into ``TOOL_FAILURE`` for
        the same reason: a partial answer with one tool missing risks
        looking complete when it isn't.
        """

        result_blocks: list[dict[str, Any]] = []
        for tool_use in turn.tool_uses:
            patient_id = str(tool_use.input.get("patient_id", ""))
            try:
                result = self._registry.dispatch(
                    tool_use.name,
                    claims=claims,
                    patient_id=patient_id,
                    request_id=request_id,
                )
            except UnauthorizedToolCallError as exc:
                return [], Abstention(
                    state=AbstentionState.UNAUTHORIZED,
                    reason=f"unauthorized access denied at tool {exc.tool_name!r}",
                )
            except ToolError as exc:
                return [], Abstention(
                    state=AbstentionState.TOOL_FAILURE,
                    reason=f"tool {tool_use.name!r} failed: {exc}",
                )
            except Exception as exc:
                return [], Abstention(
                    state=AbstentionState.TOOL_FAILURE,
                    reason=f"tool {tool_use.name!r} raised an unexpected error: {exc}",
                )

            tool_results.append(result)
            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result.model_dump_json(),
                }
            )

        next_messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": turn.raw_assistant_blocks},
            {"role": "user", "content": result_blocks},
        ]
        return next_messages, None
