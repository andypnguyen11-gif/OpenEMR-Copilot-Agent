"""Shared base for the six FHIR-backed retrieval tools.

PR 8 introduces one tool file per resource type (``meds.py``,
``allergies.py``, ``labs.py``, ``problems.py``, ``visits.py``,
``notes.py``). They share three concerns:

1. **Async ⇆ sync hand-off.** The Tool ABC is synchronous (PR 7); the
   :class:`FhirClient` is async (PR 6). Every FHIR-backed tool routes
   its async fetch through a process-wide :class:`AsyncBridge` so the
   shared ``httpx.AsyncClient`` lives on one loop.

2. **FHIR ACL → UNAUTHORIZED.** When the upstream FHIR server returns
   401 or 403 the JWT-side RBAC check already passed, so the failure is
   an ACL miss the base class translates to
   :class:`FhirAuthorizationDeniedError` (caught by :class:`Tool` and
   surfaced as :class:`UnauthorizedToolCallError` + audit row — see
   ``base.py`` for the contract). Other :class:`FhirError` subclasses
   propagate unchanged so the orchestrator's tool-failure abstention
   path can pick them up.

3. **Constructor injection of FHIR + bridge.** Tests pass a stub
   :class:`FhirClient` and an :class:`AsyncBridge` to drive the same
   code path without a network round-trip.

Projection from FHIR resource → typed :class:`AnyRecord` lives in each
concrete tool: the per-resource quirks (choice types, base64 note
bodies, lab category filters) don't share enough shape to be worth
abstracting.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

from clinical_copilot.data.fhir_client import FhirClient, FhirError
from clinical_copilot.tools.base import FhirAuthorizationDeniedError, Tool

if TYPE_CHECKING:
    from clinical_copilot.audit.log import AuditLogWriter
    from clinical_copilot.runtime.async_bridge import AsyncBridge
    from clinical_copilot.tools.records import AnyRecord


# OpenEMR returns 401 when the bearer is rejected and 403 when the bearer
# is valid but lacks scope for the requested resource. ARCHITECTURE §4
# treats both as "ACL denial" — the difference matters for ops, not for
# the abstention surface, so this layer collapses them.
_FHIR_ACL_DENIAL_STATUSES = frozenset({401, 403})


class FhirBackedTool(Tool):
    """Common base for the six concrete FHIR-backed tools."""

    def __init__(
        self,
        *,
        fhir: FhirClient,
        bridge: AsyncBridge,
        audit: AuditLogWriter,
        audit_salt: str,
    ) -> None:
        super().__init__(audit=audit, audit_salt=audit_salt)
        self._fhir = fhir
        self._bridge = bridge

    def _run(self, *, patient_id: str) -> Sequence[AnyRecord]:
        try:
            return self._bridge.run(self._fetch(patient_id=patient_id))
        except FhirError as exc:
            if exc.status_code in _FHIR_ACL_DENIAL_STATUSES:
                # ACL wins when the JWT-side check passed but the FHIR
                # server still denies — the base Tool catches this and
                # writes the same UNAUTHORIZED audit row a JWT-side
                # denial would. The cause chain keeps the upstream
                # diagnostic accessible to the orchestrator's logger.
                raise FhirAuthorizationDeniedError(str(exc)) from exc
            # Non-ACL FhirError propagates: transport, parse, OAuth, 5xx.
            # The orchestrator's _dispatch_tools collapses it into a
            # TOOL_FAILURE abstention.
            raise

    @abstractmethod
    async def _fetch(self, *, patient_id: str) -> Sequence[AnyRecord]:
        """Fetch the FHIR resources and project them into typed records.

        Subclasses do exactly two things: call the matching
        :class:`FhirClient` method and translate each parsed FHIR
        resource into the tool's :class:`AnyRecord` variant. They never
        re-check authorization (the base class did that before
        ``_run``) and never hit the audit writer (denials route through
        :class:`FhirAuthorizationDeniedError`).
        """


def reference_id(resource_type: str, resource_id: str) -> str:
    """Build the ``ResourceType/id`` string used as ``source_id``.

    The verification middleware joins on this exact format (PR 11);
    centralizing the construction here keeps every projection's
    ``source_id`` shape identical.
    """

    return f"{resource_type}/{resource_id}"
