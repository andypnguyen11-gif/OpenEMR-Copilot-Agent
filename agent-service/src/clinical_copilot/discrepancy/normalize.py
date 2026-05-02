"""Free-text + code normalization helpers for the discrepancy engine.

AUDIT D-02 calls out normalization as table-stakes. Without it the engine
drowns in false negatives — ``"Metoprolol Tartrate 50mg"`` in
``MedicationRecord.name`` and a note saying just ``"metoprolol"`` need to
match. The helpers here are the single canonical path the rules use to
compare strings, so adding a new normalization step (e.g., RxNorm-driven
brand-to-generic mapping) lands in one place.

The functions are intentionally small and side-effect free. Rules call
them; tests assert on them directly.
"""

from __future__ import annotations

import re

# Strips a trailing dose specifier like " 50 mg", " 5mcg", " 100 IU".
# We anchor on whitespace before the number so we do not mangle drugs
# whose name happens to contain a digit (e.g., "B12").
_DOSE_PATTERN = re.compile(
    r"\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|unit|units)\b",
    flags=re.IGNORECASE,
)

# Drops parentheses and commas — common in lists.title strings like
# "Amoxicillin (oral suspension), 500mg".
_PUNCT_PATTERN = re.compile(r"[(),]")

# RxNorm/ICD/SNOMED prefix matchers. We accept either the prefix-or-not
# form because OpenEMR's lists.diagnosis column is free-text and audits
# show both styles in real charts.
_CODE_SYSTEMS: tuple[str, ...] = ("rxcui", "icd10", "icd9", "snomed", "loinc")


def normalize_drug_name(raw: str) -> str:
    """Lowercase + strip dose suffix + collapse whitespace.

    Examples:

        >>> normalize_drug_name("Metoprolol Tartrate 50mg")
        'metoprolol tartrate'
        >>> normalize_drug_name("Amoxicillin 500 MG")
        'amoxicillin'
        >>> normalize_drug_name("  Lisinopril   ")
        'lisinopril'

    The output is suitable for substring matching against another
    normalized drug name. For matching against a free-text note, use
    :func:`primary_drug_token` instead — note authors rarely write the
    salt form (``"tartrate"``) so the multi-word normalized form
    matches less reliably than the leading generic stem.
    """

    if not raw:
        return ""
    cleaned = _DOSE_PATTERN.sub("", raw)
    cleaned = _PUNCT_PATTERN.sub(" ", cleaned)
    return " ".join(cleaned.lower().split())


def primary_drug_token(raw: str) -> str:
    """Return the leading generic stem from a drug name.

    The first whitespace-delimited token of :func:`normalize_drug_name`.
    Useful for note-body substring checks where authors typically write
    the bare generic (``"metoprolol"``) instead of the full salt form
    (``"metoprolol tartrate"``).

        >>> primary_drug_token("Metoprolol Tartrate 50mg")
        'metoprolol'
        >>> primary_drug_token("")
        ''
    """

    norm = normalize_drug_name(raw)
    parts = norm.split()
    return parts[0] if parts else ""


def normalize_code(raw: str) -> str | None:
    """Extract the canonical numeric/identifier portion of a coded value.

    Accepts forms like ``"RXCUI:866924"``, ``"icd10:E11.9"``, or a bare
    ``"866924"``. Returns the lowercased ``system:value`` form when a
    known system prefix is present, the bare value when no prefix is
    given, or ``None`` when input is empty / blank.

        >>> normalize_code("RXCUI:866924")
        'rxcui:866924'
        >>> normalize_code("ICD10:E11.9")
        'icd10:e11.9'
        >>> normalize_code(" 866924 ")
        '866924'
        >>> normalize_code("") is None
        True
    """

    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if ":" in cleaned:
        prefix, _, suffix = cleaned.partition(":")
        prefix_lower = prefix.strip().lower()
        suffix_clean = suffix.strip().lower()
        if prefix_lower in _CODE_SYSTEMS and suffix_clean:
            return f"{prefix_lower}:{suffix_clean}"
        # Unknown prefix or empty suffix: fall back to whole-string lower.
        return cleaned.lower()
    return cleaned.lower()


def text_contains(haystack: str, needle: str) -> bool:
    """Case-insensitive substring check after whitespace collapse.

    Tiny convenience wrapper rules use to compare a normalized drug
    token against a normalized note body without re-implementing the
    whitespace dance each time.
    """

    if not needle:
        return False
    h = " ".join(haystack.lower().split())
    n = " ".join(needle.lower().split())
    return n in h
