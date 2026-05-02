"""Tool registry — the orchestrator's only handle on the tool layer.

Holds one instance per tool class, keyed by tool name. The orchestrator
asks the registry for ``anthropic_schemas()`` to feed the SDK's tool
definition list, and ``dispatch(name, ...)`` to execute a tool the model
called. Unknown tool names raise — the model is not allowed to invoke a
name the registry doesn't know about, even by accident.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from clinical_copilot.discrepancy.cache import DiscrepancyCache
from clinical_copilot.discrepancy.chart_provider import (
    ChartProvider,
    FhirChartProvider,
    FixtureChartProvider,
)
from clinical_copilot.discrepancy.engine import DiscrepancyEngine
from clinical_copilot.discrepancy.rules import DEFAULT_PACK_PATHS, DEFAULT_REGISTRY
from clinical_copilot.observability import traceable_tool_dispatch
from clinical_copilot.tools.allergies import GetAllergiesFhirTool
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.impl import GetFlagsTool, retrieval_tool_classes
from clinical_copilot.tools.labs import GetLabsFhirTool
from clinical_copilot.tools.meds import GetMedsFhirTool
from clinical_copilot.tools.notes import GetNotesFhirTool
from clinical_copilot.tools.problems import GetProblemsFhirTool
from clinical_copilot.tools.visits import GetVisitsFhirTool

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as SqlSession
    from sqlalchemy.orm import sessionmaker

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
        chart_provider: ChartProvider | None = None,
        engine: DiscrepancyEngine | None = None,
        cache: DiscrepancyCache | None = None,
        session_factory: sessionmaker[SqlSession] | None = None,
    ) -> ToolRegistry:
        """Fixture-backed registry — six retrieval tools + ``get_flags``.

        ``chart_provider`` and ``engine`` default to a
        :class:`FixtureChartProvider` over ``store`` and the production
        :data:`DEFAULT_PACK_PATHS` rule packs. ``cache`` is the read-
        through :class:`DiscrepancyCache` ``get_flags`` reads from; if
        not supplied, one is built around the chart provider + engine,
        with the optional ``session_factory`` enabling the durable
        Postgres tier (omit it for in-process-only tests).
        """

        resolved_chart_provider = chart_provider or FixtureChartProvider(store)
        resolved_engine = engine or DiscrepancyEngine.from_yaml(
            DEFAULT_PACK_PATHS,
            DEFAULT_REGISTRY,
        )
        resolved_cache = cache or DiscrepancyCache(
            chart_provider=resolved_chart_provider,
            engine=resolved_engine,
            session_factory=session_factory,
        )

        instances: dict[str, Tool] = {}
        for tool_cls in retrieval_tool_classes():
            instances[tool_cls.name] = tool_cls(
                store=store,
                audit=audit,
                audit_salt=audit_salt,
            )
        instances[GetFlagsTool.name] = GetFlagsTool(
            cache=resolved_cache,
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
        chart_provider: ChartProvider | None = None,
        engine: DiscrepancyEngine | None = None,
        cache: DiscrepancyCache | None = None,
        session_factory: sessionmaker[SqlSession] | None = None,
    ) -> ToolRegistry:
        """Production wiring — every tool reads from the live FHIR server.

        Mirrors :meth:`from_fixture` for the FHIR-backed path: the six
        retrieval tools land alongside :class:`GetFlagsTool` reading
        through a :class:`DiscrepancyCache` over a
        :class:`FhirChartProvider`. ``session_factory`` enables the
        durable Postgres tier of the cache; in production the same
        factory is shared with the audit writer so both surfaces sit on
        one connection pool.
        """

        resolved_chart_provider = chart_provider or FhirChartProvider(
            fhir=fhir,
            bridge=bridge,
        )
        resolved_engine = engine or DiscrepancyEngine.from_yaml(
            DEFAULT_PACK_PATHS,
            DEFAULT_REGISTRY,
        )
        resolved_cache = cache or DiscrepancyCache(
            chart_provider=resolved_chart_provider,
            engine=resolved_engine,
            session_factory=session_factory,
        )

        instances: dict[str, Tool] = {}
        for tool_cls in _FHIR_TOOL_CLASSES:
            instances[tool_cls.name] = tool_cls(
                fhir=fhir,
                bridge=bridge,
                audit=audit,
                audit_salt=audit_salt,
            )
        instances[GetFlagsTool.name] = GetFlagsTool(
            cache=resolved_cache,
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

    def anthropic_schemas(
        self,
        *,
        allowed_names: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        """List of tool definitions in Anthropic SDK format.

        Returned in registry-name order so the prompt-cache key over the
        tool-defs block stays stable across requests.

        ``allowed_names`` is the per-lane subset filter. ``None`` returns
        every registered tool (slow lane). Otherwise the result is the
        intersection of the registry and the allowed set, still in
        registry-name order. An ``allowed_names`` entry that the
        registry doesn't know is silently dropped — the lane config is
        a request for "the subset of these tools that exist," not a
        contract that every name resolves. Wiring failures (an unknown
        name in a lane) surface at registry-build time, not on every
        request.
        """

        if allowed_names is None:
            return [self._tools[name].anthropic_schema() for name in self.names()]
        allowed = set(allowed_names)
        return [self._tools[name].anthropic_schema() for name in self.names() if name in allowed]

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
