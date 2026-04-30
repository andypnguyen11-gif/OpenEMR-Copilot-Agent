"""Allowlist-based PHI redaction for trace payloads.

CLAUDE.md flags PHI redaction to LangSmith as a high-risk path. Every
function in this module follows one rule: build a *new* dict from
explicitly-named safe fields, never copy the input dict and remove keys.
A denylist would silently leak the next time someone adds a free-text
field to a record type — a list nobody remembers to update is no defense
at all.

The "safe fields" surface is intentionally narrow: tool name, record
counts, source-id lists, hashed patient identifiers, latency-shaped
metadata (lengths, counts, scope counts). Anything that could be a
free-text utterance from the model, the user, or the chart is dropped.

Patient IDs are hashed with the same HMAC-SHA256 used by the audit log
(:func:`clinical_copilot.audit.log.hash_patient_id`) so a trace can be
joined to its audit row during incident response without either side
knowing the raw identifier. The salt itself is configured once at
startup via :func:`configure_redaction_salt` — every redactor reads
from the same slot so a salt rotation propagates uniformly.
"""

from __future__ import annotations

from typing import Any

from clinical_copilot.audit.log import hash_patient_id


class _SaltSlot:
    """Process-scoped salt holder.

    LangSmith's ``process_inputs`` / ``process_outputs`` signatures are
    ``(payload) -> dict`` with no place to thread configuration through.
    Rather than build per-call closures we hold the salt in a module
    slot configured once at startup. Tests don't care about the hash
    *value* (they assert raw patient_id is absent), so the dev default
    is harmless when a test runs without explicit configuration.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


_REDACTION_SALT = _SaltSlot("dev-trace-redaction-salt")


def configure_redaction_salt(salt: str) -> None:
    """Set the process-wide salt used for trace-side patient hashing.

    Called once from the composition root with the same value as the
    audit log's salt — that match is what lets investigators join a
    LangSmith trace's ``patient_id_hash`` to its audit-log row.
    """

    _REDACTION_SALT.value = salt


def _safe_hash(patient_id: object) -> str | None:
    """Hash ``patient_id`` if it looks like a non-empty string. Returns
    ``None`` for missing/empty values rather than raising — the redactor
    runs in the trace-export hot path and must never crash a request.
    """

    if not isinstance(patient_id, str) or not patient_id:
        return None
    return hash_patient_id(patient_id, salt=_REDACTION_SALT.value)


def _claims_summary(claims: object) -> dict[str, Any]:
    """Extract the three fields a trace can safely show from a
    ``ClinicianClaims`` (or any duck-typed equivalent). ``nonce`` and
    ``jti`` are JWT replay-defense identifiers — they are never traced.
    """

    user_id = getattr(claims, "user_id", None)
    role = getattr(claims, "role", None)
    patient_id = getattr(claims, "patient_id", None)
    scopes = getattr(claims, "scopes", None)
    summary: dict[str, Any] = {}
    if isinstance(user_id, str):
        summary["user_id"] = user_id
    if isinstance(role, str):
        summary["role"] = role
    if isinstance(patient_id, str) and patient_id:
        summary["session_patient_id_hash"] = hash_patient_id(
            patient_id, salt=_REDACTION_SALT.value
        )
    if isinstance(scopes, list):
        summary["scope_count"] = len(scopes)
    return summary


def redact_tool_dispatch_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Redactor for ``ToolRegistry.dispatch`` inputs.

    The dispatch signature is ``(self, name, *, claims, patient_id,
    request_id)``. We surface tool name, request_id, the hashed
    requested patient_id (which may differ from the session's bound
    patient on an RBAC denial — that delta is exactly what an incident
    investigator wants to see), plus the safe slice of claims.
    """

    redacted: dict[str, Any] = {}
    name = inputs.get("name")
    if isinstance(name, str):
        redacted["tool_name"] = name
    request_id = inputs.get("request_id")
    if isinstance(request_id, str):
        redacted["request_id"] = request_id
    requested_hash = _safe_hash(inputs.get("patient_id"))
    if requested_hash is not None:
        redacted["patient_id_hash"] = requested_hash
    redacted.update(_claims_summary(inputs.get("claims")))
    return redacted


def redact_tool_outputs(output: object) -> dict[str, Any]:
    """Redactor for the ``ToolResult`` returned by a tool dispatch.

    Every ``Record`` subclass carries free-text fields (note ``body``,
    problem ``display``, lab ``value``…). We surface only structural
    metadata and the list of server-issued ``source_id`` values — the
    same opaque identifiers the audit log already records. ``None`` is
    returned when the wrapped function raised.
    """

    if output is None:
        return {"record_count": 0, "source_ids": []}

    tool_name = getattr(output, "tool_name", None)
    patient_id = getattr(output, "patient_id", None)
    records = getattr(output, "records", None) or []

    redacted: dict[str, Any] = {
        "record_count": len(records),
        "source_ids": [
            sid
            for sid in (getattr(record, "source_id", None) for record in records)
            if isinstance(sid, str)
        ],
    }
    if isinstance(tool_name, str):
        redacted["tool_name"] = tool_name
    patient_hash = _safe_hash(patient_id)
    if patient_hash is not None:
        redacted["patient_id_hash"] = patient_hash
    return redacted


def redact_llm_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Redactor for ``LlmGateway.complete`` inputs.

    ``system`` is the system prompt (may contain prompt-injection
    countermeasure phrasing — not PHI itself, but volume-y and
    irrelevant for traces). ``messages`` carries the *entire* per-turn
    conversation, including tool-result blocks whose ``content`` is the
    JSON-serialized tool output we just scrubbed elsewhere. Both must be
    reduced to lengths and counts.
    """

    redacted: dict[str, Any] = {}
    system = inputs.get("system")
    if isinstance(system, str):
        redacted["system_prompt_length"] = len(system)
    tools = inputs.get("tools") or []
    if isinstance(tools, list):
        redacted["tool_def_names"] = [
            tool["name"]
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]
    messages = inputs.get("messages") or []
    if isinstance(messages, list):
        redacted["message_count"] = len(messages)
        redacted["message_roles"] = [
            msg["role"]
            for msg in messages
            if isinstance(msg, dict) and isinstance(msg.get("role"), str)
        ]
    return redacted


def redact_llm_outputs(output: object) -> dict[str, Any]:
    """Redactor for the ``LlmTurn`` an LLM call returns.

    The model's free-form text is the largest single PHI risk in the
    pipeline (it can quote the chart verbatim). We surface its length
    only. Tool-use blocks are safe to enumerate by name — those are
    model-chosen identifiers, not chart content.
    """

    if output is None:
        return {
            "stop_reason": None,
            "text_length": 0,
            "tool_use_names": [],
            "tool_use_count": 0,
        }

    text = getattr(output, "text", "") or ""
    tool_uses = getattr(output, "tool_uses", None) or []
    tool_use_names = [
        getattr(use, "name", None)
        for use in tool_uses
        if isinstance(getattr(use, "name", None), str)
    ]
    return {
        "stop_reason": getattr(output, "stop_reason", None),
        "text_length": len(text),
        "tool_use_names": tool_use_names,
        "tool_use_count": len(tool_uses),
    }


def redact_orchestrator_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Redactor for ``Orchestrator.run`` inputs.

    The user's natural-language ``query`` is the top-level PHI risk —
    physicians type patient names, dates, complaints. We surface its
    length and the safe claims slice; nothing else.
    """

    redacted: dict[str, Any] = {}
    query = inputs.get("query")
    if isinstance(query, str):
        redacted["query_length"] = len(query)
    request_id = inputs.get("request_id")
    if isinstance(request_id, str):
        redacted["request_id"] = request_id
    claims_summary = _claims_summary(inputs.get("claims"))
    if "session_patient_id_hash" in claims_summary:
        # Promote the session patient hash to a top-level field so
        # request-level traces can be filtered by it without descending
        # into the claims summary block.
        redacted["patient_id_hash"] = claims_summary["session_patient_id_hash"]
    redacted.update(claims_summary)
    return redacted


def redact_orchestrator_outputs(output: object) -> dict[str, Any]:
    """Redactor for the final ``AgentResponse``.

    Cards carry titles (``"Active problems"`` — fine) plus source-id
    lists, but we drop titles to avoid drift if a future card kind ever
    encodes patient text in a title (cheaper to enforce here than to
    re-prove the safety property each time a kind is added). Prose
    ``text`` is the highest-risk field and is reduced to a count.
    """

    if output is None:
        return {
            "card_count": 0,
            "prose_count": 0,
            "tool_result_count": 0,
            "abstention_state": None,
        }

    cards = getattr(output, "cards", None) or []
    prose = getattr(output, "prose", None) or []
    tool_results = getattr(output, "tool_results", None) or []
    abstention = getattr(output, "abstention", None)

    abstention_state: str | None = None
    if abstention is not None:
        state = getattr(abstention, "state", None)
        # ``AbstentionState`` is a ``StrEnum``; ``str()`` is the wire value.
        abstention_state = str(state) if state is not None else None

    return {
        "card_count": len(cards),
        "prose_count": len(prose),
        "tool_result_count": len(tool_results),
        "abstention_state": abstention_state,
    }
