"""Tool registry — the orchestrator's only handle on the tool layer.

Holds one instance per tool class, keyed by tool name. The orchestrator
asks the registry for ``anthropic_schemas()`` to feed the SDK's tool
definition list, and ``dispatch(name, ...)`` to execute a tool the model
called. Unknown tool names raise — the model is not allowed to invoke a
name the registry doesn't know about, even by accident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from clinical_copilot.observability import traceable_tool_dispatch
from clinical_copilot.tools.allergies import GetAllergiesFhirTool
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.impl import all_tool_classes
from clinical_copilot.tools.labs import GetLabsFhirTool
from clinical_copilot.tools.meds import GetMedsFhirTool
from clinical_copilot.tools.notes import GetNotesFhirTool
from clinical_copilot.tools.problems import GetProblemsFhirTool
from clinical_copilot.tools.visits import GetVisitsFhirTool

if TYPE_CHECKING:
    from clinical_copilot.audit.log import AuditLogWriter
    from clinical_copilot.auth.session import ClinicianClaims
    from clinical_copilot.data.fhir_client import FhirClient
    from clinical_copilot.runtime.async_bridge import AsyncBridge
    from clinical_copilot.tools.base import Tool
    from clinical_copilot.tools.fhir_base import FhirBackedTool
    from clinical_copilot.tools.records import ToolResult


_FHIR_TOOL_CLASSES: tuple[type[FhirBackedTool], ...] = (
    GetProblemsFhirTool,
    GetMedsFhirTool,
    GetAllergiesFhirTool,
    GetLabsFhirTool,
    GetVisitsFhirTool,
    GetNotesFhirTool,
)


class UnknownToolError(Exception):
    """Raised when the model emits a ``tool_use`` block for a name the
    registry does not have. Caller surfaces this as a tool-failure
    abstention; we never silently drop the call.
    """


class ToolRegistry:
    """Process-local map from tool name to a configured tool instance.

    One :class:`ToolRegistry` per app, built at startup. Tests build a
    registry with a stub :class:`FixtureStore` and a stub
    :class:`AuditLogWriter` so the same dispatch path runs without a
    Postgres connection.
    """

    def __init__(self, tools: dict[str, Tool]) -> None:
        self._tools = tools

    @classmethod
    def from_fixture(
        cls,
        *,
        store: FixtureStore,
        audit: AuditLogWriter,
        audit_salt: str,
    ) -> ToolRegistry:
        instances: dict[str, Tool] = {}
        for tool_cls in all_tool_classes():
            instances[tool_cls.name] = tool_cls(
                store=store,
                audit=audit,
                audit_salt=audit_salt,
            )
        return cls(instances)

    @classmethod
    def from_fhir(
        cls,
        *,
        fhir: FhirClient,
        bridge: AsyncBridge,
        audit: AuditLogWriter,
        audit_salt: str,
    ) -> ToolRegistry:
        """Production wiring — every tool reads from the live FHIR server.

        ``get_flags`` is intentionally absent: PR 13 owns the flag
        surface and the rules engine that populates it. Until then, the
        FHIR-backed registry exposes only the six retrieval tools
        listed in PR 8 / ARCHITECTURE §1.
        """

        instances: dict[str, Tool] = {}
        for tool_cls in _FHIR_TOOL_CLASSES:
            instances[tool_cls.name] = tool_cls(
                fhir=fhir,
                bridge=bridge,
                audit=audit,
                audit_salt=audit_salt,
            )
        return cls(instances)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(name) from exc

    def anthropic_schemas(self) -> list[dict[str, object]]:
        """List of tool definitions in Anthropic SDK format.

        Returned in registry-name order so the prompt-cache key over the
        tool-defs block stays stable across requests.
        """

        return [self._tools[name].anthropic_schema() for name in self.names()]

    @traceable_tool_dispatch
    def dispatch(
        self,
        name: str,
        *,
        claims: ClinicianClaims,
        patient_id: str,
        request_id: str,
    ) -> ToolResult:
        return self.get(name).execute(
            claims=claims,
            patient_id=patient_id,
            request_id=request_id,
        )
