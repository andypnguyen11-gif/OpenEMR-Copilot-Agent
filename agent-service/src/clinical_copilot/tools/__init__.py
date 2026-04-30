"""Tool layer for the Clinical Co-Pilot agent.

Each tool is a thin retrieval primitive over a single resource type, gated by
a per-tool RBAC check that compares the request's :class:`ClinicianClaims`
(from the verified gateway JWT) against the requested ``patient_id``. When
the check fails, the base class writes an ``UNAUTHORIZED`` row to the
fail-closed audit log before raising — the model can never read PHI for a
patient outside the session's scope, and no denial leaves the building
without a trail.

For M1 the underlying data source is the hand-encoded fixture in
``agent-service/tests/fixtures/patients.json``. PR 6 swaps the fixture for
live FHIR; the Tool ABC contract is stable across that swap.
"""
