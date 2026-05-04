"""Pre-LLM check that catches obvious cross-patient asks.

The slow-lane and fast-lane prompts both tell the model to refuse when
the user's question references a patient other than the one bound to
the session. The model honors this most of the time but not always —
when it slips, ``get_meds`` (and friends) still fetch the *bound*
patient's records (the registry is patient-scoped at call time, so the
PHI isolation guarantee holds), but the model presents those records
as if they answered the user's question. The clinician sees the wrong
patient's data labeled as the right one.

This module is the deterministic guard. Before the LLM loop runs we
extract any ``patient X`` / ``X's <noun>`` patterns from the query and
compare against the bound patient's name parts. If the user named a
patient that isn't this session's patient, the orchestrator short-
circuits to a NO_DATA abstention with a clear reason.

Scope is deliberately narrow:

* Only the two explicit syntactic patterns above. Free-floating
  Title-Case tokens are ignored — too many false positives from drug
  names, condition names, and sentence-initial verbs.
* No NER, no LLM, no fuzzy matching. The bound name comparison is a
  case-insensitive substring check against the bound patient's name
  parts (``given`` + ``family``).
* Returns ``None`` when no name appears in the query at all — that is
  the common case, not a refusal trigger.
"""

from __future__ import annotations

import re

# Capture group #1: the proper-noun token following ``patient``.
# Matches: ``patient Queenie``, ``Patient Maria's``, ``patients Maria``.
# Does not match: ``the patient is``, ``patient who``, etc. (lowercase
# token after ``patient`` is not a name). The capital-letter requirement
# on the captured name MUST stay case-sensitive — applying IGNORECASE
# would let ``patient who`` capture ``"who"`` and trigger every query
# that uses the word ``patient`` in its colloquial sense.
_PATIENT_PREFIX_RE = re.compile(r"\b[Pp]atients?\s+([A-Z][A-Za-z]{2,})", re.ASCII)

# Capture group #1: a Title-Case token followed by an apostrophe-s
# possessive (curly or straight). Matches: ``Queenie's meds``,
# ``Maria's labs``. Does not match: ``patient's chart`` (lowercase),
# ``BP's trend`` (single uppercase letter run, not a name shape).
_POSSESSIVE_RE = re.compile(r"\b([A-Z][a-z]{2,})['’]s\b", re.ASCII)


def extract_referenced_names(query: str) -> list[str]:
    """Return Title-Case names the query explicitly attaches to a patient.

    Two patterns:

    * ``patient X`` / ``patients X`` — the literal word ``patient`` (or
      ``patients``) followed by a Title-Case token.
    * ``X's`` — a Title-Case token in the possessive form.

    Both patterns require the token to start with an uppercase letter
    and be at least three characters. That filters out the bulk of
    accidental matches (``He``, ``It``, ``Or``) without an allowlist.

    Output preserves first-seen order and deduplicates case-insensitively
    so the caller can report a stable list.
    """

    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_PATIENT_PREFIX_RE, _POSSESSIVE_RE):
        for match in pattern.finditer(query):
            name = match.group(1)
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(name)
    return found


def cross_patient_check(query: str, bound_patient_name: str | None) -> str | None:
    """Return an abstention reason if the query names a different patient.

    Returns ``None`` for every safe shape:

    * The query mentions no patient name (the typical case).
    * The query mentions a name that matches a part of the bound
      patient's name — exact substring, case-insensitive, in either
      direction (``"Maria"`` matches bound ``"Maria Lopez"``; bound
      ``"Maria"`` matches query ``"Maria Lopez"``).
    * ``bound_patient_name`` is missing — without a comparator we can't
      decide and the LLM-side prompt rule remains the only line of
      defense. Logged at the call site, not here.

    Returns a short reason string when the query names a patient that
    cannot be matched to the bound patient. The orchestrator wraps this
    into a typed ``NO_DATA`` abstention.
    """

    referenced = extract_referenced_names(query)
    if not referenced:
        return None
    if not bound_patient_name:
        return None

    bound_lower = bound_patient_name.lower()
    bound_parts = {part for part in bound_lower.split() if len(part) >= 3}

    mismatches: list[str] = []
    for name in referenced:
        name_lower = name.lower()
        # Match if the query name is a substring of the bound name, OR
        # if any bound-name part is a substring of the query name. Either
        # direction handles the cases where one side is the full name and
        # the other is just a first name.
        if name_lower in bound_lower or any(part in name_lower for part in bound_parts):
            continue
        mismatches.append(name)

    if not mismatches:
        return None

    if len(mismatches) == 1:
        named = repr(mismatches[0])
    else:
        named = ", ".join(repr(m) for m in mismatches)

    return (
        f"query references patient {named} but this session is bound to "
        f"{bound_patient_name!r} — open the other patient's chart to ask about them"
    )
