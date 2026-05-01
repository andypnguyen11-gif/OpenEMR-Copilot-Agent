"""Stub for OpenEMR's non-FHIR REST surface.

ARCHITECTURE §5 carves out a non-FHIR REST client because OpenEMR exposes
some clinical data (audit-log writes, certain admin reads) only on the
``/api/`` REST surface, not on FHIR. The full audit of which endpoints
the agent needs lives in PR 19 (audit-log writes) and surfaces gaps
PR 6 doesn't yet have a consumer for — adding speculative methods here
would be the kind of "designed for hypothetical future requirements"
this codebase explicitly avoids (see CLAUDE.md).

Concretely: this module exists so the import path
``clinical_copilot.data.rest_client`` resolves and so the wiring in
``main.py`` can construct a placeholder. Real methods land in the PR
that introduces a caller; until then, the class is empty by design.
"""

from __future__ import annotations

import httpx


class RestClient:
    """Placeholder for the OpenEMR non-FHIR REST surface.

    Constructed with the same ``base_url`` / ``http_client`` shape as
    :class:`clinical_copilot.data.fhir_client.FhirClient` so the wiring
    is symmetric once methods land.
    """

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be non-empty")
        self._base_url = base_url.rstrip("/")
        self._http = http_client
