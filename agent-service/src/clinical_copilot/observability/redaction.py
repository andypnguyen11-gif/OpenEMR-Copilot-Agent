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

import re
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


# Regex backstop patterns. The allowlist redactors above already drop
# free-text fields wholesale; this layer is belt-and-suspenders for PHI
# that slips through inside an *allowed* field — e.g. an MRN baked into
# a model-chosen ``tool_name``, an email accidentally promoted to
# ``user_id``, or a future record schema growing a string identifier
# whose contents weren't audited. Each match is replaced with a typed
# placeholder so a trace reader sees that scrubbing happened (and which
# kind fired) rather than silently losing characters.
_PHI_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\bMRN[:\s#]*\d{4,}\b", re.IGNORECASE), "MRN"),
    (re.compile(r"(?<!\d)\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"), "PHONE"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "EMAIL"),
    (
        re.compile(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])[/-](19|20)\d{2}\b"),
        "DOB",
    ),
    (
        re.compile(r"\b(19|20)\d{2}-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b"),
        "DOB",
    ),
    (
        re.compile(r"\"(family|given)\"\s*:\s*(\"[^\"]+\"|\[[^\]]*\])"),
        "FHIR_NAME",
    ),
)


def _scrub_phi_patterns(text: str) -> str:
    """Run the regex backstop over a single string value.

    Returns the input unchanged when no pattern matches. The replacement
    is ``[REDACTED:<KIND>]`` so a trace reader can see the kind of PHI
    that was scrubbed without exposing the value itself.
    """

    if not text:
        return text
    scrubbed = text
    for pattern, kind in _PHI_PATTERNS:
        scrubbed = pattern.sub(f"[REDACTED:{kind}]", scrubbed)
    return scrubbed


def _scrub_value(value: Any) -> Any:
    """Recursive walker over arbitrarily-nested redactor output.

    Strings get the regex backstop; dicts and lists get walked
    element-wise; non-string scalars pass through. Returns ``Any`` so
    mypy does not narrow the polymorphic value type — top-level callers
    use :func:`_scrub_payload` to preserve the ``dict[str, Any]``
    contract at the redactor boundary.
    """

    if isinstance(value, str):
        return _scrub_phi_patterns(value)
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    return value


def _scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Top-level dict-typed entry point used by every redactor.

    The redactors return plain dicts of strings, ints, lists, and other
    dicts — every string value is a candidate for the backstop. This
    wrapper preserves the ``dict[str, Any]`` return type at the
    boundary so each redactor's signature stays narrow.
    """

    return {key: _scrub_value(value) for key, value in payload.items()}


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
        summary["session_patient_id_hash"] = hash_patient_id(patient_id, salt=_REDACTION_SALT.value)
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
    return _scrub_payload(redacted)


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
    return _scrub_payload(redacted)


def redact_llm_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Redactor for ``LlmGateway.complete`` inputs.

    ``system`` is the system prompt (may contain prompt-injection
    countermeasure phrasing — not PHI itself, but volume-y and
    irrelevant for traces). ``messages`` carries the *entire* per-turn
    conversation, including tool-result blocks whose ``content`` is the
    JSON-serialized tool output we just scrubbed elsewhere. Both must be
    reduced to lengths and counts.

    The model identifier is read from the bound ``self`` LangSmith
    captures for ``@traceable`` methods. Surfacing it lets traces be
    filtered by lane (slow lane runs the larger model, fast lane the
    smaller) without descending into the parent orchestrator span.
    """

    redacted: dict[str, Any] = {}
    gateway = inputs.get("self")
    model = getattr(gateway, "model", None)
    if isinstance(model, str):
        redacted["model"] = model
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
    return _scrub_payload(redacted)


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
    return _scrub_payload(
        {
            "stop_reason": getattr(output, "stop_reason", None),
            "text_length": len(text),
            "tool_use_names": tool_use_names,
            "tool_use_count": len(tool_uses),
        }
    )


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
    lane = inputs.get("lane")
    if lane is not None:
        # ``Lane`` is a ``StrEnum`` so its wire value is just ``str(lane)``;
        # tolerate plain strings too in case a future caller passes one.
        redacted["lane"] = str(lane)
    claims_summary = _claims_summary(inputs.get("claims"))
    if "session_patient_id_hash" in claims_summary:
        # Promote the session patient hash to a top-level field so
        # request-level traces can be filtered by it without descending
        # into the claims summary block.
        redacted["patient_id_hash"] = claims_summary["session_patient_id_hash"]
    redacted.update(claims_summary)
    return _scrub_payload(redacted)


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

    return _scrub_payload(
        {
            "card_count": len(cards),
            "prose_count": len(prose),
            "tool_result_count": len(tool_results),
            "abstention_state": abstention_state,
        }
    )


# ---------------------------------------------------------------------------
# Supervisor (LangGraph) node redactors
# ---------------------------------------------------------------------------


def _usage_totals_summary(value: object) -> dict[str, int] | None:
    """Coerce a ``UsageTotals`` dataclass or a dict-shaped fold result
    into the same wire shape. Returns ``None`` for missing/unknown
    inputs so callers can decide whether to omit the key.
    """

    if value is None:
        return None
    input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "output_tokens", None)
    if input_tokens is None and isinstance(value, dict):
        input_tokens = value.get("input_tokens")
        output_tokens = value.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _str_value(value: object) -> str | None:
    """Return ``value`` if it's a non-empty string, else ``None``.
    Used for enum-shaped fields that may arrive as ``StrEnum`` or raw
    string depending on whether the dict came through ``model_dump``.
    """

    if isinstance(value, str) and value:
        return value
    return None


def _draft_summary(draft: object) -> dict[str, Any]:
    """Pull only structural fields off a Draft-shaped dict / model.

    ``text`` is the synthesizer's prose for a sub-query and is dropped
    wholesale. ``citations`` source_id / corpus_id are server-issued
    opaque identifiers (matching the orchestrator redactor's policy)
    so they pass through to support trace-side debugging of which
    chart records or guideline chunks a draft cited.
    """

    citations: list[dict[str, Any]] = []
    raw_citations = (
        draft.get("citations") if isinstance(draft, dict) else getattr(draft, "citations", None)
    )
    if raw_citations:
        for citation in raw_citations:
            if isinstance(citation, dict):
                source_id = citation.get("source_id")
                corpus_id = citation.get("corpus_id")
            else:
                source_id = getattr(citation, "source_id", None)
                corpus_id = getattr(citation, "corpus_id", None)
            entry: dict[str, Any] = {}
            if isinstance(source_id, str):
                entry["source_id"] = source_id
            if isinstance(corpus_id, str):
                entry["corpus_id"] = corpus_id
            if entry:
                citations.append(entry)
    worker = (
        draft.get("worker") if isinstance(draft, dict) else getattr(draft, "worker", None)
    )
    abstain_reason = (
        draft.get("abstain_reason")
        if isinstance(draft, dict)
        else getattr(draft, "abstain_reason", None)
    )
    return {
        "worker": _str_value(worker) if worker is not None else None,
        "abstain_reason": _str_value(abstain_reason),
        "citations": citations,
    }


def _verdict_summary(verdict: object) -> dict[str, Any]:
    """Pull only the closed-enum fields off a Verdict. ``rationale``
    is critic free-text — sometimes quotes the draft prose — and is
    dropped wholesale.
    """

    raw_verdict = (
        verdict.get("verdict") if isinstance(verdict, dict) else getattr(verdict, "verdict", None)
    )
    raw_reason = (
        verdict.get("rejection_reason")
        if isinstance(verdict, dict)
        else getattr(verdict, "rejection_reason", None)
    )
    return {
        "verdict": _str_value(raw_verdict),
        "rejection_reason": _str_value(raw_reason),
    }


def redact_supervisor_node_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Redactor for LangGraph supervisor node *inputs*.

    LangGraph nodes are called with one positional ``state`` argument
    (a :class:`TurnState` TypedDict). ``@traceable`` captures kwargs by
    name so this function receives ``{"state": <state_dict>}``. Anything
    else in ``inputs`` is something a future caller added (extra
    config, callbacks) — drop it.

    The state dict carries the highest-risk PHI in the system: the
    user's natural-language query, the patient's name on
    ``session``, and every worker draft's prose. The allowlist
    surfaces structural metadata (counts, request_id, hashed patient
    identifier, token totals) and drops every free-text field.
    """

    state = inputs.get("state")
    if not isinstance(state, dict):
        state = {}

    redacted: dict[str, Any] = {}

    session = state.get("session")
    if isinstance(session, dict):
        request_id = session.get("request_id")
        if isinstance(request_id, str):
            redacted["request_id"] = request_id
        patient_id_hash = _safe_hash(session.get("patient_id"))
        if patient_id_hash is not None:
            redacted["patient_id_hash"] = patient_id_hash

    user_query = state.get("user_query")
    if isinstance(user_query, str):
        redacted["user_query_length"] = len(user_query)

    sub_queries = state.get("sub_queries") or []
    redacted["sub_query_count"] = len(sub_queries) if isinstance(sub_queries, list) else 0

    drafts = state.get("drafts") or []
    redacted["draft_count"] = len(drafts) if isinstance(drafts, list) else 0

    verdicts = state.get("verdicts") or []
    redacted["verdict_count"] = len(verdicts) if isinstance(verdicts, list) else 0

    rerank_backend = state.get("rerank_backend")
    if isinstance(rerank_backend, str):
        redacted["rerank_backend"] = rerank_backend

    usage = _usage_totals_summary(state.get("usage_totals"))
    if usage is not None:
        redacted["usage_totals"] = usage

    retry_counts = state.get("retry_counts")
    if isinstance(retry_counts, dict):
        # Sub-query IDs are uuid hex (planner-assigned, see SubQuery.id);
        # ints are counts. Neither carries PHI; pass through.
        redacted["retry_counts"] = {
            str(key): int(val)
            for key, val in retry_counts.items()
            if isinstance(val, int)
        }

    return _scrub_payload(redacted)


def redact_supervisor_node_outputs(output: object) -> dict[str, Any]:
    """Redactor for LangGraph supervisor node *outputs*.

    Each node returns a partial-state dict that LangGraph folds into
    the running :class:`TurnState`. Different nodes write different
    keys — synthesizer writes ``final_response`` + ``usage_totals``,
    workers write ``drafts``, the planner writes ``sub_queries``, the
    critic writes ``verdicts``. The allowlist below covers every
    key any node currently writes; an unknown key is dropped (defense
    in depth — a future node that returns a PHI-bearing field will
    not silently leak just because nobody updated this redactor).
    """

    if not isinstance(output, dict):
        return {}

    redacted: dict[str, Any] = {}

    final_response = output.get("final_response")
    if isinstance(final_response, dict):
        synthesized_text = final_response.get("synthesized_text") or ""
        abstention_reason = final_response.get("abstention_reason")
        redacted["synthesized_text_length"] = (
            len(synthesized_text) if isinstance(synthesized_text, str) else 0
        )
        redacted["abstention_reason"] = (
            abstention_reason if isinstance(abstention_reason, str) else None
        )

    drafts = output.get("drafts")
    if isinstance(drafts, list):
        summaries = [_draft_summary(draft) for draft in drafts]
        redacted["draft_count"] = len(summaries)
        redacted["draft_workers"] = [
            summary["worker"] for summary in summaries if summary["worker"] is not None
        ]
        redacted["draft_abstain_reasons"] = [summary["abstain_reason"] for summary in summaries]
        redacted["draft_citations"] = [summary["citations"] for summary in summaries]

    verdicts = output.get("verdicts")
    if isinstance(verdicts, list):
        summaries = [_verdict_summary(verdict) for verdict in verdicts]
        redacted["verdict_count"] = len(summaries)
        redacted["verdicts"] = summaries

    sub_queries = output.get("sub_queries")
    if isinstance(sub_queries, list):
        claim_types: list[str] = []
        for sub_query in sub_queries:
            raw = (
                sub_query.get("claim_type")
                if isinstance(sub_query, dict)
                else getattr(sub_query, "claim_type", None)
            )
            value = _str_value(raw)
            if value is not None:
                claim_types.append(value)
        redacted["sub_query_count"] = len(sub_queries)
        redacted["sub_query_claim_types"] = claim_types

    rerank_backend = output.get("rerank_backend")
    if isinstance(rerank_backend, str):
        redacted["rerank_backend"] = rerank_backend

    usage = _usage_totals_summary(output.get("usage_totals"))
    if usage is not None:
        redacted["usage_totals"] = usage

    retry_counts = output.get("retry_counts")
    if isinstance(retry_counts, dict):
        redacted["retry_counts"] = {
            str(key): int(val)
            for key, val in retry_counts.items()
            if isinstance(val, int)
        }

    return _scrub_payload(redacted)
