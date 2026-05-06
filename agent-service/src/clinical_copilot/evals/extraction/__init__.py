"""Stage 4A extraction eval — 50 cases, boolean rubrics, regression gate.

Public surface used by the runner CLI and unit tests:

* :class:`Case` — typed manifest entry, exact-50 loader.
* :class:`Label` — human-reviewed expectations for a single case.
* :func:`evaluate` — run the five boolean rubrics over a case + facts.
* :func:`load_baseline` / :func:`check_regression` — gate against
  ``baseline.json``.
* :func:`run_gate` — runner entrypoint wired to ``make eval-extraction-gate``.

Everything else (CSV/JSON/Markdown writers, PHI scan) is internal.
"""

from __future__ import annotations
