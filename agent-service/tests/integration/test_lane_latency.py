"""Fast-lane latency budget against real Anthropic.

PRD §13 commits the in-chart side panel (fast lane) to a p50 wall-clock
of ≤5s on warm cache. This test verifies that budget against the real
Anthropic API. Skipped without ``ANTHROPIC_API_KEY``: CI without the
secret can't run it cleanly, and the assertion is meaningless without
the real network round-trip.

Shape:

* 1 warm-up turn priming Anthropic's prompt cache for the
  (system_fast.md, fast-lane tool defs) prefix.
* 5 measured turns issuing the same query against fixture-backed tools.
* Assert ``statistics.median(samples) <= 5.0`` so a single noisy run
  doesn't fail the suite — the PRD budget is p50, not max.
* Print every sample so a flake stands out as one bad run vs systemic.

The test runs against the in-process orchestrator (no FastAPI route, no
network for tool calls) so the only variable is the Anthropic round-trip
itself — exactly what the budget governs. When PR 13's flag rules
engine lands a real cache, the warm-up turn will need to seed it
before measurement begins; today flags come from the in-memory
``FixtureStore`` so the "warm flags cache" half is automatic.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Iterator

import pytest
from anthropic import Anthropic

from clinical_copilot.app_state import _SYSTEM_FAST_PATH, _SYSTEM_SLOW_PATH
from clinical_copilot.audit.log import AuditLogWriter
from clinical_copilot.audit.models import AuditEvent
from clinical_copilot.auth.session import ClinicianClaims
from clinical_copilot.config import DEFAULT_MODEL_FAST, DEFAULT_MODEL_SLOW
from clinical_copilot.orchestrator.agent import Orchestrator
from clinical_copilot.orchestrator.lanes import Lane, LaneConfig
from clinical_copilot.orchestrator.llm_gateway import AnthropicLlmGateway
from clinical_copilot.orchestrator.sessions import SessionStore
from clinical_copilot.tools.fixtures import FixtureStore
from clinical_copilot.tools.registry import ToolRegistry
from clinical_copilot.verification.middleware import VerificationMiddleware

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not ANTHROPIC_API_KEY,
        reason="ANTHROPIC_API_KEY not set — skipping latency budget verification",
    ),
]

LATENCY_BUDGET_SECONDS = 5.0
WARMUP_TURNS = 1
MEASURED_TURNS = 5

FAST_LANE_TOOLS = frozenset({"get_flags", "get_problems", "get_meds", "get_visits"})


class _NullAudit(AuditLogWriter):
    """Audit writes are a no-op for this latency test — we don't care
    about the trail, only the wall-clock. The audit-fail-closed contract
    lives in its own unit test.
    """

    def __init__(self) -> None:
        # Skip the parent's session-factory wiring; ``write`` is a no-op
        # so the underlying engine is never touched.
        pass

    def write(self, event: AuditEvent) -> None:
        return None


@pytest.fixture
def claims() -> ClinicianClaims:
    return ClinicianClaims(
        user_id="dr-patel",
        role="physician",
        # Patient 102 in the fixture has non-empty flags so the
        # flag-first prompt has something real to surface — closer to
        # the production fast-lane shape than an empty result.
        patient_id="102",
        scopes=[
            "system/Condition.read",
            "system/MedicationRequest.read",
            "system/AllergyIntolerance.read",
            "system/Observation.read",
            "system/Encounter.read",
            "system/DocumentReference.read",
        ],
        nonce="latency-nonce",
        jti="latency-jti",
    )


@pytest.fixture
def orch() -> Iterator[Orchestrator]:
    """Two-lane orchestrator wired to real Anthropic + fixture tools."""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    slow_llm = AnthropicLlmGateway(
        client=client,
        model=os.environ.get("MODEL_SLOW", DEFAULT_MODEL_SLOW),
    )
    fast_llm = AnthropicLlmGateway(
        client=client,
        model=os.environ.get("MODEL_FAST", DEFAULT_MODEL_FAST),
    )

    registry = ToolRegistry.from_fixture(
        store=FixtureStore.from_file(),
        audit=_NullAudit(),
        audit_salt="latency-salt",
    )
    sessions = SessionStore()
    verifier = VerificationMiddleware()
    yield Orchestrator(
        lanes={
            Lane.SLOW: LaneConfig(
                llm=slow_llm,
                system_prompt=_SYSTEM_SLOW_PATH.read_text(encoding="utf-8"),
                tool_names=None,
            ),
            Lane.FAST: LaneConfig(
                llm=fast_llm,
                system_prompt=_SYSTEM_FAST_PATH.read_text(encoding="utf-8"),
                tool_names=FAST_LANE_TOOLS,
            ),
        },
        registry=registry,
        verifier=verifier,
        sessions=sessions,
    )


def test_fast_lane_p50_under_budget(
    orch: Orchestrator,
    claims: ClinicianClaims,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Warm Anthropic's prompt cache, then assert median wall-clock for
    five fast-lane turns is ≤5s.

    The session id is omitted on every turn so each measurement starts
    from a fresh session — multi-turn message growth would skew later
    samples without telling us anything new about the fast-lane budget.
    """

    query = "Anything I should know about this patient right now?"

    # Warm-up: the first call to a (model, tool-defs, system) triple
    # writes Anthropic's prompt cache. The PRD budget is the warm-cache
    # number, so we burn one turn to prime and discard the timing.
    for _ in range(WARMUP_TURNS):
        orch.run(
            query=query,
            claims=claims,
            request_id="warmup",
            lane=Lane.FAST,
        )

    samples: list[float] = []
    for i in range(MEASURED_TURNS):
        start = time.perf_counter()
        orch.run(
            query=query,
            claims=claims,
            request_id=f"measure-{i}",
            lane=Lane.FAST,
        )
        samples.append(time.perf_counter() - start)

    # Print every sample so a one-off flake is visually obvious in the
    # test output. ``capsys`` plus ``-s`` shows them; without ``-s``
    # they're still in the captured output for failure diagnostics.
    with capsys.disabled():
        print(f"\nfast-lane samples (s): {[round(s, 3) for s in samples]}")
        print(f"  median: {statistics.median(samples):.3f}s")
        print(f"  budget: {LATENCY_BUDGET_SECONDS:.1f}s")

    p50 = statistics.median(samples)
    assert p50 <= LATENCY_BUDGET_SECONDS, (
        f"fast-lane p50 {p50:.3f}s exceeds {LATENCY_BUDGET_SECONDS:.1f}s budget; samples: {samples}"
    )
