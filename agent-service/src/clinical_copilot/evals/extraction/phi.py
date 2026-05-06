"""PHI sentinel scan for written eval result files.

The :data:`SSN_RE` pattern and :func:`scan_text` helper are the *same*
trust surface used by the per-case ``no_phi_in_logs`` rubric in
``rubrics.py`` (rubric inlines for performance; this module is the
canonical definition).

Used by:

* ``rubrics._check_no_phi_in_logs`` (per-case, in-memory output).
* ``runner.py`` after writing ``results/<run_id>.json`` — a defense-
  in-depth sweep over the bytes that actually hit disk, in case any
  rubric regression let a PHI-shaped string slip past the per-case
  check.

Per-case forbidden tokens come from ``EvalOutput.forbidden_phi`` (the
case manifest can declare patient-specific tokens that must never
appear in synthesis).
"""

from __future__ import annotations

import re
from pathlib import Path

SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class PhiLeakError(RuntimeError):
    """Raised when the post-write PHI sweep finds a sentinel."""


def scan_text(text: str, *, forbidden_tokens: tuple[str, ...] = ()) -> list[str]:
    """Return a list of PHI hits in ``text``. Empty list = clean.

    Hits are returned as short human-readable strings ("SSN-shape:
    ###-##-####" or "forbidden token: <token>") so the runner can log
    them without re-extracting context.
    """

    hits: list[str] = []
    for match in SSN_RE.finditer(text):
        hits.append(f"SSN-shape: {match.group(0)}")
    lower = text.lower()
    for token in forbidden_tokens:
        if token and token.lower() in lower:
            hits.append(f"forbidden token: {token}")
    return hits


def scan_results_file(path: Path, *, forbidden_tokens: tuple[str, ...] = ()) -> list[str]:
    """Read ``path`` and run :func:`scan_text` over its full contents."""

    return scan_text(path.read_text(), forbidden_tokens=forbidden_tokens)
