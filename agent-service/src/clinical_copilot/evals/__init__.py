"""Eval-side package — keep separate from runtime by import-linter contract.

Runtime modules under ``clinical_copilot.documents`` /
``clinical_copilot.orchestrator`` / ``clinical_copilot.verification`` must
not import from this package, and this package must not import from
``clinical_copilot.tests`` or any test-only fixture surface. The
boundary keeps eval rubric drift from leaking into production paths.
"""

from __future__ import annotations
