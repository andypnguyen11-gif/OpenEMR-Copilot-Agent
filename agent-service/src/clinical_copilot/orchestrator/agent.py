"""Single-loop tool-using orchestrator.

ARCHITECTURE §1.2. The flow per request is:

1. Send the user query + system prompt + tool defs to the LLM.
2. While the LLM emits ``tool_use`` blocks, dispatch each through
   :class:`ToolRegistry` (which performs the per-tool RBAC check) and
   feed the typed results back as ``tool_result`` blocks.
3. When the LLM emits a final text turn, parse it as
   :class:`ModelDraft`. One schema-violation retry; a second failure
   becomes a ``VERIFICATION_FAILED`` whole-response abstention.
4. Run the draft through :class:`VerificationMiddleware`. The middleware
   either passes the draft through or replaces it with an abstention.

Failure modes mapped to abstention states:

* RBAC denial from any tool → ``UNAUTHORIZED`` (audit row already
  written by the tool layer — see ``Tool._enforce_rbac``).
* Tool raised any other error → ``TOOL_FAILURE``.
* Loop exceeded ``max_turns`` → ``TOOL_FAILURE`` ("agent could not
  converge"); shielding against runaway tool loops.
* Final JSON failed schema validation twice → ``VERIFICATION_FAILED``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from clinical_copilot.orchestrator.llm_gateway import LlmGateway, LlmTurn
from clinical_copilot.orchestrator.schemas import AgentResponse, ModelDraft
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

DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.md"
DEFAULT_MAX_TURNS = 8


class Orchestrator:
    """One orchestrator per app, configured at startup."""

    def __init__(
        self,
        *,
        llm: LlmGateway,
        registry: ToolRegistry,
        verifier: VerificationMiddleware,
        system_prompt: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._verifier = verifier
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT_PATH.read_text(
            encoding="utf-8"
        )
        self._max_turns = max_turns

    def run(
        self,
        *,
        query: str,
        claims: ClinicianClaims,
        request_id: str,
    ) -> AgentResponse:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": query},
        ]
        tool_results: list[ToolResult] = []
        retried = False

        for _ in range(self._max_turns):
            turn = self._llm.complete(
                system=self._system_prompt,
                tools=self._registry.anthropic_schemas(),
                messages=messages,
            )

            if turn.tool_uses:
                tool_messages, abstention = self._dispatch_tools(
                    turn=turn,
                    claims=claims,
                    request_id=request_id,
                    tool_results=tool_results,
                )
                if abstention is not None:
                    return AgentResponse(
                        cards=[],
                        prose=[],
                        tool_results=tool_results,
                        abstention=abstention,
                    )
                messages.extend(tool_messages)
                continue

            text = turn.text.strip()
            try:
                draft = ModelDraft.model_validate_json(text)
            except ValidationError as exc:
                if retried:
                    return AgentResponse(
                        cards=[],
                        prose=[],
                        tool_results=tool_results,
                        abstention=Abstention(
                            state=AbstentionState.VERIFICATION_FAILED,
                            reason="model emitted JSON that failed schema validation twice",
                        ),
                    )
                retried = True
                messages.append({"role": "assistant", "content": turn.raw_assistant_blocks})
                messages.append(
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

            return self._verifier.verify(draft=draft, tool_results=tool_results)

        return AgentResponse(
            cards=[],
            prose=[],
            tool_results=tool_results,
            abstention=Abstention(
                state=AbstentionState.TOOL_FAILURE,
                reason=f"agent did not converge within {self._max_turns} turns",
            ),
        )

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
