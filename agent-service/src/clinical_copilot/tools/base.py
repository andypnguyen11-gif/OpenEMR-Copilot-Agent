"""Tool ABC + RBAC enforcement + audit hook.

Every PHI fetch routes through :meth:`Tool.execute`. The base class enforces
three layers of authorization, all *before* ``_run`` is invoked:

1. **Role**, against ``Tool.allowed_roles``. Default allow-list is the three
   known clinical roles ``{PHYSICIAN, RESIDENT, SUPERVISOR}``;
   :attr:`Role.UNKNOWN` is denied by omission. Subclasses can narrow the set
   (e.g. supervisor-only tools) by overriding the ClassVar.
2. **Patient id**, against the JWT-bound ``claims.patient_id``. A model that
   emits a foreign id (prompt-injection probe) trips here, not after the
   fetch.
3. **Scope**, against ``claims.scopes``. The tool's ``required_scope`` must
   be present.

Any miss → UNAUTHORIZED audit row → :class:`UnauthorizedToolCallError`,
before ``_run`` runs.

A fourth layer kicks in *after* the fetch attempt:

4. **FHIR ACL**. ``_run`` may raise :class:`FhirAuthorizationDeniedError` to
   signal the upstream FHIR server returned 401 / 403. The base catches
   that, writes the same UNAUTHORIZED audit row, and re-raises as
   :class:`UnauthorizedToolCallError` chained from the original. ACL wins
   when the layers disagree (ARCHITECTURE §4).

All four denial branches produce identical audit and exception surfaces so
the orchestrator's abstention layer has one shape to handle.

When ``_run`` returns normally, the base writes a ``SUCCESS`` audit row
*before* handing the records back to the caller. ARCHITECTURE §7 (line
521 / §8.3) treats audit-log integrity as higher priority than
availability: every PHI access must produce a row, and a write failure
fails the request rather than leaking the data un-logged. Non-RBAC
exceptions raised by ``_run`` (parse errors, transport faults) propagate
untouched and do *not* write a SUCCESS row — those are faults, not
PHI accesses. The audit table thus holds exactly one row per resolved
access: SUCCESS or UNAUTHORIZED, never both, never none.

The audit write is fail-closed via :class:`AuditLogWriter`: a write error
surfaces as :class:`AuditLogWriteError` and the request returns 5xx —
never SUCCESS or UNAUTHORIZED without a logged row.

ARCHITECTURE §4 puts the JWT-side checks on the *tool side* of the trust
boundary so neither the model nor the gateway can leak chart data by
fabricating claims past the verifier.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.role import Role
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.tools.records import AnyRecord, ToolResult

DEFAULT_ALLOWED_ROLES: frozenset[Role] = frozenset(
    {Role.PHYSICIAN, Role.RESIDENT, Role.SUPERVISOR},
)


class ToolError(Exception):
    """Base for tool-side failures the orchestrator translates into
    abstention states. Subclasses carry no caller-facing message — the
    abstention layer composes user-visible copy from the state, not from
    exception text.
    """


class UnauthorizedToolCallError(ToolError):
    """Raised when RBAC denies a tool call.

    The audit row is written *before* this is raised, so a caller seeing
    this exception can rely on the trail being persisted. Fail-closed:
    if the audit write itself raises :class:`AuditLogWriteError`, that
    surfaces instead and the request returns 5xx — never UNAUTHORIZED
    without a logged row.
    """

    def __init__(self, tool_name: str, *, requested_patient_id: str) -> None:
        super().__init__(f"unauthorized tool call: {tool_name}")
        self.tool_name = tool_name
        self.requested_patient_id = requested_patient_id


class FhirAuthorizationDeniedError(ToolError):
    """Raised by ``_run`` when the upstream FHIR server denies the read.

    The JWT-side RBAC check already passed (otherwise execution would not
    have reached ``_run``), but the OAuth-bearer call to FHIR came back
    401 / 403. ARCHITECTURE §4 makes the FHIR ACL the authority: when the
    two layers disagree, ACL wins. The base class catches this, writes an
    UNAUTHORIZED audit row, and re-raises as
    :class:`UnauthorizedToolCallError` — subclasses do not write the
    audit row themselves.

    PR 7 ships the contract; PR 8's FHIR-backed tools are the first
    concrete callers (mapping ``FhirError`` with status 401 / 403 into
    this exception inside ``_run``).
    """


class Tool(ABC):
    """ABC for retrieval tools.

    Subclasses set ``name``, ``description``, ``required_scope`` as class
    vars and implement :meth:`_run`. The base class handles RBAC and
    audit; subclasses never reach for ``claims`` or the audit writer
    directly.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    required_scope: ClassVar[str]
    record_kind: ClassVar[str]
    # Default allow-list covers the three MVP clinical roles. Subclasses
    # narrow this for role-restricted endpoints (the supervisor audit-log
    # read tool will set ``frozenset({Role.SUPERVISOR})``). UNKNOWN is
    # denied by omission, not by special-case — keep it that way so a
    # future role added to the enum doesn't silently inherit access.
    allowed_roles: ClassVar[frozenset[Role]] = DEFAULT_ALLOWED_ROLES

    def __init__(self, *, audit: AuditLogWriter, audit_salt: str) -> None:
        self._audit = audit
        self._audit_salt = audit_salt

    def execute(
        self,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> ToolResult:
        self._enforce_rbac(
            claims=claims,
            patient_id=patient_id,
            request_id=request_id,
        )
        try:
            records = self._run(patient_id=patient_id)
        except FhirAuthorizationDeniedError as exc:
            # JWT side passed, FHIR ACL disagreed. ACL wins
            # (ARCHITECTURE §4). Audit the denial against the *requested*
            # patient_id (the target of the failed access) and surface the
            # same UnauthorizedToolCallError the JWT-side path raises so
            # the orchestrator's abstention layer has one shape to handle.
            self._audit.write(
                self._unauthorized_event(
                    claims=claims,
                    patient_id=patient_id,
                    request_id=request_id,
                )
            )
            raise UnauthorizedToolCallError(
                self.name,
                requested_patient_id=patient_id,
            ) from exc
        # Success-path audit. Fail-closed: an AuditLogWriteError here
        # propagates and the caller sees no records — ARCHITECTURE §7
        # (line 521) prefers audit integrity over availability. Other
        # _run exceptions already escaped above; they are not PHI
        # accesses and intentionally do not produce a SUCCESS row.
        self._audit.write(
            self._success_event(
                claims=claims,
                patient_id=patient_id,
                request_id=request_id,
            )
        )
        return ToolResult(
            tool_name=self.name,
            patient_id=patient_id,
            records=list(records),
        )

    @abstractmethod
    def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
        """Return the typed records for ``patient_id``.

        Implementations must not perform authorization — by the time
        ``_run`` runs, the base class has already verified the call.
        """

    @classmethod
    def anthropic_schema(cls) -> dict[str, object]:
        """Tool definition payload for the Anthropic SDK.

        All M1 tools share a single input shape (``patient_id``), so the
        base produces it from class metadata. Tools that grow inputs
        later override.
        """

        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "The patient identifier to fetch. Must match the "
                            "session's authorized patient — supplying any other "
                            "value will be denied at the tool layer and audit-logged."
                        ),
                    },
                },
                "required": ["patient_id"],
                "additionalProperties": False,
            },
        }

    def _enforce_rbac(
        self,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> None:
        role_ok = claims.role in self.allowed_roles
        patient_match = patient_id == claims.patient_id
        scope_ok = self.required_scope in claims.scopes
        if role_ok and patient_match and scope_ok:
            return
        self._audit.write(
            self._unauthorized_event(
                claims=claims,
                patient_id=patient_id,
                request_id=request_id,
            )
        )
        raise UnauthorizedToolCallError(self.name, requested_patient_id=patient_id)

    def _unauthorized_event(
        self,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> AuditEvent:
        return self._audit_event(
            claims=claims,
            patient_id=patient_id,
            request_id=request_id,
            action="UNAUTHORIZED",
        )

    def _success_event(
        self,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> AuditEvent:
        return self._audit_event(
            claims=claims,
            patient_id=patient_id,
            request_id=request_id,
            action="SUCCESS",
        )

    def _audit_event(
        self,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
        action: str,
    ) -> AuditEvent:
        return AuditEvent(
            user_id=claims.user_id,
            # AuditEvent stores the wire format (string), not the enum —
            # the column is plain text in the audit-log table and rotates
            # independently of the in-process Role enum's case set.
            role=claims.role.value,
            patient_id_hash=hash_patient_id(patient_id, salt=self._audit_salt),
            resource_type=self.name,
            action=action,
            request_id=request_id,
        )
