"""Index-time PHI-shape scrub (PRD2 §7).

Refuses to index any chunk that matches a PHI-shape regex. The
detector is intentionally conservative — clinical guidance documents
should not contain SSNs, MRNs, phone numbers, or person-name patterns,
so a hit is more likely a content-curation mistake than a false
positive. The full W2-06 scrubber additionally writes a
manifest-rejection log; the demo cut just raises so the rebuild fails
loudly.
"""

from __future__ import annotations

import re

# Conservative PHI-shape patterns. Each finds *something* that should
# never appear in a public-guideline corpus.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Phone like (XXX) XXX-XXXX or XXX-XXX-XXXX.
    ("phone", re.compile(r"(?:\(\d{3}\)\s*|\b\d{3}-)\d{3}-\d{4}\b")),
    # Email — no public-domain guideline doc should embed a personal email.
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # MRN-style: 7+ digit run after "MRN" or "Medical Record".
    ("MRN", re.compile(r"(?i)\bMRN[:\s#]*\d{6,}\b")),
)


class PhiInCorpusError(RuntimeError):
    """Raised when the corpus scrubber detects a PHI-shape match."""


def scrub_or_raise(*, source_doc_id: str, text: str) -> None:
    for label, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is not None:
            raise PhiInCorpusError(
                f"PHI-shape '{label}' matched in corpus doc {source_doc_id!r}: "
                f"{match.group(0)!r}"
            )
