"""Tool ABC + RBAC enforcement + UNAUTHORIZED audit hook.

Every PHI fetch routes through :meth:`Tool.execute`. The base class enforces
two checks before letting the subclass touch data:

1. ``patient_id`` requested in the call equals ``claims.patient_id`` (the
   session-bound patient pinned by the gateway JWT).
2. The tool's ``required_scope`` is in ``claims.scopes``.

Either failure produces an ``UNAUTHORIZED`` audit-log row (fail-closed via
:class:`AuditLogWriter`) and raises :class:`UnauthorizedToolCallError`. The
caller — the orchestrator's tool dispatch — translates the exception into
the response-level ``UNAUTHORIZED`` abstention state. ARCHITECTURE §4 puts
this check on the *tool side* of the trust boundary so a model that emits
a foreign ``patient_id`` (prompt-injection probe) trips RBAC before the
fetch happens, not after.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from clinical_copilot.audit.log import AuditLogWriter, hash_patient_id
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.tools.records import AnyRecord, ToolResult


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
        records = self._run(patient_id=patient_id)
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
        patient_match = patient_id == claims.patient_id
        scope_ok = self.required_scope in claims.scopes
        if patient_match and scope_ok:
            return
        self._audit.write(
            AuditEvent(
                user_id=claims.user_id,
                role=claims.role,
                patient_id_hash=hash_patient_id(patient_id, salt=self._audit_salt),
                resource_type=self.name,
                action="UNAUTHORIZED",
                request_id=request_id,
            )
        )
        raise UnauthorizedToolCallError(self.name, requested_patient_id=patient_id)
