"""Co-Pilot clinical role.

Mirror of the PHP gateway's ``OpenEMR\\Services\\Copilot\\Auth\\Role`` enum.
The role string travels across the trust boundary inside the JWT's ``role``
claim; both sides parse it into their respective enum so the rest of the code
works against a closed type rather than free-form strings.

``StrEnum`` (Python 3.11+) keeps the comparison ergonomics clean: a value
unmarshalled from JSON compares equal to ``Role.PHYSICIAN`` without an
explicit cast, and serialising back to JSON yields the same wire format the
PHP gateway emits.

The agent service's tool layer keys per-tool scope eligibility off this
enum (added in the next slice). For now, the only callers parse JWT claims
and stamp the role into audit rows; ``Role.UNKNOWN`` exists as the explicit
"resolver couldn't classify" sentinel and is denied by the tool layer's
default-no-scopes policy.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Clinical role discriminator carried in the JWT ``role`` claim.

    Values match :class:`OpenEMR\\Services\\Copilot\\Auth\\Role` exactly —
    any drift between the two enums turns into a silent ``UNKNOWN`` fallback
    (see :meth:`from_claim`) and a denied request, not a crash.
    """

    UNKNOWN = "unknown"
    PHYSICIAN = "physician"
    RESIDENT = "resident"
    SUPERVISOR = "supervisor"

    @classmethod
    def from_claim(cls, value: str) -> Role:
        """Parse the JWT ``role`` claim into the enum.

        Unrecognised values resolve to :attr:`UNKNOWN` rather than raising.
        The verifier already accepted the token's signature; refusing to
        parse a future role string here would convert a benign forward-
        compatibility case (gateway adds ``"FELLOW"`` before the agent
        service ships its matching enum) into a 5xx. UNKNOWN preserves
        deny-by-default at the tool layer without that hazard.
        """

        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN
