"""Eval-case shapes, loading, and the assertion engine.

A case is a JSON file under ``tests/eval/cases/<category>/<id>.json``. The
schema is intentionally narrow — every field maps to one assertion the
runner makes against an :class:`AgentResponse` returned over HTTP. The
shape is closed (Pydantic ``extra="forbid"``) so a typo in a case file
fails loud at load time rather than silently weakening a check.

The trust-critical assertion is :meth:`Expectation.evaluate`'s handling
of ``forbidden_source_id_regex``: any source_id appearing in
``tool_results``, ``cards``, or ``prose`` that matches the regex is a
hard failure. The RBAC-bypass case wires this against ``p999-`` so a
response leaking out-of-panel data fails regardless of whether the
agent abstained, refused, or answered.

JWT signing here mirrors the test_query_route helper (PR M3) so the
deployed service's HS256 verifier accepts harness-issued tokens
identically to gateway-issued ones.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt as pyjwt
from pydantic import BaseModel, ConfigDict, Field

# Reuse the gateway's issuer/audience constants so a harness JWT is
# byte-compatible with one minted by the PHP gateway.
from clinical_copilot.auth.jwt_verifier import ALGORITHM, AUDIENCE, ISSUER

CASES_DIR = Path(__file__).resolve().parent / "cases"
RBAC_CATEGORY = "rbac_bypass"
DEFAULT_JWT_TTL_SECONDS = 60

# Buckets the snapshot script (``scripts/snapshot_eval_patients.py``) writes
# into ``eval-patient-ids.json``. Cases reference one of these by name; an
# unknown bucket fails loud at load time so a typo doesn't silently degrade
# to "no patient resolved" at request time.
KNOWN_BUCKETS = frozenset({"full_chart", "no_allergies", "no_problems", "default"})


class EvalCaseLoadError(ValueError):
    """Raised when a case JSON references state the loader cannot resolve.

    Distinct from pydantic's :class:`ValidationError` (which fires on shape
    violations) so a missing snapshot file or an empty bucket reads as a
    setup problem, not a schema bug.
    """


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Session(_Frozen):
    """The principal under which the case runs.

    Mirrors :class:`ClinicianClaims`. The harness signs a JWT carrying
    these fields; the deployed verifier turns them back into the same
    claims object the orchestrator and tool layer consume.
    """

    user_id: str
    role: str
    patient_id: str
    scopes: list[str]


class Expectation(_Frozen):
    """Closed-shape assertion bundle.

    Every field is optional except ``abstention_state_in`` — at minimum a
    case must declare which abstention states are acceptable so the
    runner doesn't silently pass on an unexpected outcome.
    """

    abstention_state_in: list[str | None]
    any_source_id_prefix: list[str] | None = None
    any_prose_keyword_ci: list[str] | None = None
    forbidden_source_id_regex: str | None = None
    forbidden_prose_regex_ci: str | None = None


class EvalCase(_Frozen):
    case_id: str = Field(alias="id", min_length=1)
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    query: str = Field(min_length=1)
    session: Session
    expect: Expectation

    @property
    def is_rbac_gate(self) -> bool:
        return self.category == RBAC_CATEGORY


@dataclass(frozen=True, slots=True)
class CaseFailure:
    reason: str


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """Result of evaluating one case against one response.

    ``failures`` is empty on a pass. Multiple failures may be reported
    from a single case (e.g. wrong abstention *and* a forbidden source
    id) so the runner output is informative on a single run.
    """

    case: EvalCase
    failures: tuple[CaseFailure, ...]
    raw_response: dict[str, Any] | None
    transport_error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.transport_error is None


def load_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load the bucket map written by ``snapshot_eval_patients.py``.

    Returns just the ``buckets`` payload — the surrounding metadata
    (``generated_at``, ``openemr_url``) is informational and never
    consulted by the resolver.
    """

    if not path.is_file():
        raise EvalCaseLoadError(
            f"snapshot file not found: {path} "
            "(run scripts/snapshot_eval_patients.py first)"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalCaseLoadError(f"snapshot {path} is not valid JSON: {exc}") from exc
    buckets = payload.get("buckets")
    if not isinstance(buckets, dict):
        raise EvalCaseLoadError(
            f"snapshot {path} missing 'buckets' object — re-run the snapshot script"
        )
    return buckets


def _resolve_patient_ref(
    payload: dict[str, Any],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None,
    source: Path,
) -> None:
    """If the case references a snapshot bucket, replace it in-place with a pid.

    Operates on the raw JSON dict before pydantic validation so the
    :class:`Session` schema can stay closed-shape (``patient_id: str``).
    A literal-string ``patient_id`` is left untouched — keeps the M5
    fixture-driven cases working without edit.
    """

    session = payload.get("session")
    if not isinstance(session, dict):
        return
    ref = session.get("patient_id")
    if not isinstance(ref, dict):
        return

    if snapshot is None:
        raise EvalCaseLoadError(
            f"{source.name} references a patient bucket but no snapshot was loaded "
            "(pass --snapshot to runner.py or load_snapshot() to load_cases)"
        )

    bucket = ref.get("bucket")
    if not isinstance(bucket, str) or bucket not in KNOWN_BUCKETS:
        raise EvalCaseLoadError(
            f"{source.name}: unknown bucket {bucket!r}; "
            f"expected one of {sorted(KNOWN_BUCKETS)}"
        )

    index = ref.get("index", 0)
    if not isinstance(index, int) or index < 0:
        raise EvalCaseLoadError(
            f"{source.name}: bucket index must be a non-negative int, got {index!r}"
        )

    entries = snapshot.get(bucket) or []
    if index >= len(entries):
        raise EvalCaseLoadError(
            f"{source.name}: bucket {bucket!r} has {len(entries)} patient(s); "
            f"index {index} out of range — re-run the snapshot script with more imports"
        )

    entry = entries[index]
    uuid = entry.get("uuid") if isinstance(entry, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise EvalCaseLoadError(
            f"{source.name}: snapshot entry for {bucket}[{index}] missing 'uuid' — "
            "snapshot file is malformed"
        )
    session["patient_id"] = uuid


def load_cases(
    root: Path = CASES_DIR,
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> list[EvalCase]:
    """Load every ``*.json`` under ``root`` into typed cases.

    Files are sorted by path so output ordering is stable across runs —
    important for CI diffs and for the demo recording (PR M6).

    ``snapshot`` (from :func:`load_snapshot`) resolves bucket-referenced
    patient ids. Cases that hardcode ``patient_id`` as a string don't
    need a snapshot; cases that reference a bucket fail loud if it's
    missing.
    """

    if not root.is_dir():
        raise FileNotFoundError(f"eval cases directory not found: {root}")

    cases: list[EvalCase] = []
    for path in sorted(root.rglob("*.json")):
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        _resolve_patient_ref(payload, snapshot=snapshot, source=path)
        cases.append(EvalCase.model_validate(payload))
    return cases


def mint_jwt(
    *,
    session: Session,
    secret: str,
    ttl_seconds: int = DEFAULT_JWT_TTL_SECONDS,
) -> str:
    """Sign an HS256 JWT the deployed verifier will accept.

    Each call generates a fresh ``jti`` and ``nonce`` so replay-store
    rejections don't fire when running the suite back-to-back.
    """

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
        "nonce": uuid.uuid4().hex,
        "user_id": session.user_id,
        "role": session.role,
        "patient_id": session.patient_id,
        "scopes": list(session.scopes),
    }
    return pyjwt.encode(payload, secret, algorithm=ALGORITHM)


def evaluate(case: EvalCase, response_body: dict[str, Any]) -> tuple[CaseFailure, ...]:
    """Run every assertion declared on ``case`` against the response body.

    Returns the tuple of failures. Empty tuple == pass. The walk over
    source_ids covers ``tool_results.records[].source_id`` (the
    server-attested set), ``cards[].source_ids`` (UI surface), and
    ``prose[].source_id`` (model-cited). A forbidden id leaking through
    *any* of those is a violation.
    """

    failures: list[CaseFailure] = []
    abstention = response_body.get("abstention")
    abstention_state = abstention.get("state") if isinstance(abstention, dict) else None

    if abstention_state not in case.expect.abstention_state_in:
        failures.append(
            CaseFailure(
                reason=(
                    f"expected abstention_state in {case.expect.abstention_state_in!r}, "
                    f"got {abstention_state!r}"
                )
            )
        )

    all_source_ids = list(_iter_source_ids(response_body))
    prose_text = _join_prose(response_body)

    if case.expect.forbidden_source_id_regex is not None:
        pattern = re.compile(case.expect.forbidden_source_id_regex)
        leaked = [sid for sid in all_source_ids if pattern.search(sid)]
        if leaked:
            failures.append(
                CaseFailure(
                    reason=(
                        f"response cited forbidden source_ids {leaked!r} "
                        f"(pattern={case.expect.forbidden_source_id_regex!r})"
                    )
                )
            )

    if case.expect.forbidden_prose_regex_ci is not None:
        pattern_ci = re.compile(case.expect.forbidden_prose_regex_ci, re.IGNORECASE)
        if pattern_ci.search(prose_text):
            failures.append(
                CaseFailure(
                    reason=(
                        f"prose matched forbidden pattern {case.expect.forbidden_prose_regex_ci!r}"
                    )
                )
            )

    # Positive assertions are only meaningful when the agent actually
    # produced an answer. If the case allows an abstention and one
    # fired, skip them — an abstention that is permitted is a pass.
    if abstention_state is None:
        if case.expect.any_source_id_prefix is not None:
            prefixes = case.expect.any_source_id_prefix
            if not any(_starts_with_any(sid, prefixes) for sid in all_source_ids):
                failures.append(
                    CaseFailure(
                        reason=(
                            f"no cited source_id matched any prefix in {prefixes!r}; "
                            f"saw {sorted(set(all_source_ids))!r}"
                        )
                    )
                )

        if case.expect.any_prose_keyword_ci is not None:
            keywords = case.expect.any_prose_keyword_ci
            haystack = prose_text.lower()
            if not any(kw.lower() in haystack for kw in keywords):
                failures.append(
                    CaseFailure(
                        reason=(f"no prose keyword from {keywords!r} appeared in {prose_text!r}")
                    )
                )

    return tuple(failures)


def _iter_source_ids(body: dict[str, Any]) -> Iterable[str]:
    for tool_result in body.get("tool_results") or []:
        for record in tool_result.get("records") or []:
            sid = record.get("source_id")
            if isinstance(sid, str):
                yield sid
    for card in body.get("cards") or []:
        for sid in card.get("source_ids") or []:
            if isinstance(sid, str):
                yield sid
    for claim in body.get("prose") or []:
        sid = claim.get("source_id")
        if isinstance(sid, str):
            yield sid


def _join_prose(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for claim in body.get("prose") or []:
        text = claim.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _starts_with_any(value: str, prefixes: list[str]) -> bool:
    return any(value.startswith(p) for p in prefixes)
