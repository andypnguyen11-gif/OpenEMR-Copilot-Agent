"""Citation existence check.

Layer 3 of ARCHITECTURE §3. Every ``source_id`` the model writes — in
``CitedClaim.source_id`` and in ``Card.source_ids`` — must resolve to a
record the agent actually fetched (i.e. appears in the union of
``ToolResult.records[*].source_id``).

There is no "infer support from a partial match" path. A fabricated
source_id is the canonical fabrication failure mode; the response-level
abstention is conservative on purpose.
"""

from __future__ import annotations

from collections.abc import Iterable

from clinical_copilot.orchestrator.schemas import Card, CitedClaim
from clinical_copilot.tools.records import ToolResult


def collect_source_ids(tool_results: Iterable[ToolResult]) -> set[str]:
    """Return every server-attested ``source_id`` from the fetched
    records. Used both by the citation check and by tests that want to
    assert no orphan IDs remain in the response.
    """

    ids: set[str] = set()
    for result in tool_results:
        for record in result.records:
            ids.add(record.source_id)
    return ids


def find_unresolved_citations(
    *,
    claims: Iterable[CitedClaim],
    cards: Iterable[Card],
    tool_results: Iterable[ToolResult],
) -> list[str]:
    """Return ``source_id`` values cited in the draft but not present in
    any ``tool_results`` record.

    Order is preserved-ish: claim-side IDs first (in iteration order),
    then card-side IDs. The ordering is not load-bearing for the trust
    decision (any non-empty result fails the response), but stable
    output makes test failures legible.
    """

    fetched = collect_source_ids(tool_results)
    missing: list[str] = []
    seen: set[str] = set()

    for claim in claims:
        if claim.source_id not in fetched and claim.source_id not in seen:
            missing.append(claim.source_id)
            seen.add(claim.source_id)

    for card in cards:
        for source_id in card.source_ids:
            if source_id not in fetched and source_id not in seen:
                missing.append(source_id)
                seen.add(source_id)

    return missing
