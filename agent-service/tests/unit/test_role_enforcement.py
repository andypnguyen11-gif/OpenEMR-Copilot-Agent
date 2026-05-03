"""Tests for the agent-service Role enum and JWT-claim parsing.

The PHP gateway resolves the clinician's role from OpenEMR's ``users`` table
(``physician_type`` + ``supervisor_id``), serialises the resulting
:class:`Role` value into the JWT's ``role`` claim, and the agent-service
verifier hands back a string. This file pins the next hop: that string must
parse cleanly back into the matching Python :class:`Role` enum so the tool
layer (the next slice) can key per-role scope eligibility off a closed type
rather than free-form strings.

The cross-language enum contract is what these tests actually defend. If the
PHP enum gains a value the Python enum doesn't know about, :meth:`from_claim`
must resolve to :attr:`Role.UNKNOWN` rather than raise — the verifier
already accepted the token's signature, and converting forward-compat to a
5xx would surface as a service outage to a clinician whose role we simply
hadn't shipped yet. UNKNOWN preserves the deny-by-default property at the
tool boundary without that hazard.
"""

from __future__ import annotations

from clinical_copilot.auth.role import Role


def test_enum_values_match_php_gateway_wire_format() -> None:
    # The string values are the JWT contract with the PHP gateway's matching
    # OpenEMR\Services\Copilot\Auth\Role enum. Any rename here breaks
    # round-trip until both sides ship together — pin the values explicitly.
    assert Role.UNKNOWN.value == "unknown"
    assert Role.PHYSICIAN.value == "physician"
    assert Role.RESIDENT.value == "resident"
    assert Role.SUPERVISOR.value == "supervisor"


def test_from_claim_parses_every_known_value() -> None:
    for role in Role:
        assert Role.from_claim(role.value) is role


def test_from_claim_returns_unknown_for_unrecognised_value() -> None:
    # Forward-compatibility: a future PHP enum case (e.g. "fellow") must
    # not crash the verifier. Resolves to UNKNOWN; the tool layer denies
    # UNKNOWN at the next boundary so the request still fails closed.
    assert Role.from_claim("fellow") is Role.UNKNOWN
    assert Role.from_claim("") is Role.UNKNOWN
    assert Role.from_claim("PHYSICIAN") is Role.UNKNOWN  # case-sensitive


def test_strenum_allows_natural_string_comparison() -> None:
    # StrEnum gives us == comparison against the raw claim string without
    # an explicit cast. This matters because the verifier currently keeps
    # the role as a plain ``str`` on ClinicianClaims; downstream code that
    # has converted to Role enum should still compare equal to claim
    # strings unmarshalled from older JWT payloads cached during a deploy.
    assert Role.PHYSICIAN == "physician"
    assert Role.RESIDENT == "resident"
