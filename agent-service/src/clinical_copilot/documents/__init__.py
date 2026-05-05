"""Week 2 document ingestion + extraction (PRD2 §6, W2_ARCHITECTURE §5).

This package owns the path from "a binary lives in OpenEMR Documents" to
"typed, citation-bearing facts live in agent-db". Week 1 did not have
this path at all — chart facts came from FHIR-only structured tools.
Week 2 layers structured-document extraction on top of that without
changing the Week 1 surface.
"""

from __future__ import annotations
