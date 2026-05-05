"""Cross-cutting type contracts for Week 2 (PRD2 Appendix A.1, A.5).

The Week 1 module surface lives under ``clinical_copilot.<package>``; the
``schemas`` package is the Week 2 home for runtime types that multiple
later packages must share without creating cross-package coupling
(``documents``, ``corpus``, ``orchestrator`` all import from here, but
none of them import from each other).
"""

from __future__ import annotations
