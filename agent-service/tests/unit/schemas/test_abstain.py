"""Tests for the canonical RuntimeAbstainReason enum."""

from __future__ import annotations

from clinical_copilot.schemas.abstain import RuntimeAbstainReason
from clinical_copilot.verification.abstention import AbstentionState


def test_runtime_abstain_reason_has_all_seven_canonical_members() -> None:
    expected = {
        "NO_DATA",
        "VERIFICATION_FAILED",
        "TOOL_FAILURE",
        "UNAUTHORIZED",
        "LOW_CONFIDENCE",
        "OUT_OF_SCHEMA",
        "CITATION_INVALID",
    }
    assert {m.name for m in RuntimeAbstainReason} == expected
    # String values match member names — wire serialization contract.
    assert {m.value for m in RuntimeAbstainReason} == expected


def test_v1_abstention_state_alias_is_the_new_enum() -> None:
    # Week 1 callers (`AbstentionState.NO_DATA`) must keep working
    # without source changes. The alias is the same enum, not a copy.
    assert AbstentionState is RuntimeAbstainReason
    assert AbstentionState.NO_DATA is RuntimeAbstainReason.NO_DATA


def test_runtime_abstain_reason_str_round_trips() -> None:
    # StrEnum guarantees the string value equals the member's str().
    for member in RuntimeAbstainReason:
        assert str(member) == member.value
        assert RuntimeAbstainReason(member.value) is member
