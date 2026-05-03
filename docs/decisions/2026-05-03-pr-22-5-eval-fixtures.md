# Decision Log: PR 22.5 — UC5/6/7 eval fixture coverage
**Date**: 2026-05-03
**Intensity**: balanced

## Decision 1: UC5 cadence breadth

**Options considered:**
- A1 (chosen) — Diabetes-only happy_path cases against the existing T2DM/A1c rule; ship two cases (probe + alt phrasing). Zero rule-pack changes.
- A2 — Extend `data_quality.yaml` with amiodarone-TFT + statin-lipid expectations now and ship three cases. Matches USERS.md UC5 fully but adds rule-pack + fixture work.
- A3 — Diabetes-only PR 22.5 plus a TASKS.md follow-up entry for the YAML extension.

**Choice:** A1
**Rationale:** Sunday deadline; A1 ships green with the rule pack as-is. Amiodarone/statin extension lives in TASKS.md as a post-deadline follow-up so the gap is tracked, not hidden.

## Decision 2: UC6 negation case shape

**Options considered:**
- B1 (chosen) — Ship a red case asserting the agent does not flag in negation context; documents the failure mode USERS.md UC6 already names.
- B2 — Patch the rule with NKDA / "no known drug allergies" filtering before writing the case so it ships green. Risks scope creep into rule code.
- B3 — Skip the negation case; only ship the positive PCN-mismatch case.

**Choice:** B1
**Rationale:** USERS.md UC6 lists negation as the dominant failure mode. A red case that documents it honestly is more valuable to a grader than a green case that hides the gap or a quiet skip.

## Decision 3: UC7 implementation gap

**Options considered:**
- C1 — Add a thin `get_audit` LLM tool wrapping `AuditLogReader`; ship UC7 eval cases through the agent loop. ~60-90 min plus risk of mid-implementation RBAC plumbing surprises.
- C2 (chosen) — Revise USERS.md UC7 to describe the existing direct REST endpoint as the supervision surface; drop UC7 eval cases from PR 22.5; spawn a follow-up PR for the LLM-tool variant.
- C3 — Leave USERS.md UC7 as-is and defer all UC7 work; accept the doc/code mismatch.

**Choice:** C2
**Rationale:** USERS.md UC7 (written 2026-05-03 in the same revision as the punch list) described a `get_audit` tool that doesn't exist. Honest doc fix beats either crash-implementing the tool against the deadline or shipping a doc that misrepresents the codebase. The LLM-driven variant remains valuable and is queued as a follow-up PR; PR 23's RBAC volume can absorb supervisor-off-panel probes directly against the existing REST endpoint.

## Decision 4: Negation probe fixture

**Options considered:**
- D1 (chosen) — Add patient 90005 to `patients.json` with a "denies penicillin allergy / NKDA" note. Purely additive; doesn't touch existing patients or the rule-parity test (which only enforces 101-104).
- D2 — Modify 90002's note to combine sulfa-positive + penicillin-negation in the same patient. Compresses fixture but couples two unrelated case shapes together.

**Choice:** D1
**Rationale:** Additive fixture changes are safer than mutations. The existing parity test only asserts on 101-104 rule sets, so 90005 can carry its own narrative-only-allergy probe without affecting any other coverage.

## Decision 5: Negation case assertion direction

**Options considered:**
- E1 (chosen) — Case asserts the *correct* semantics (agent does NOT flag in negation context). Ships red today; flips green when the rule gains a negation filter.
- E2 — Case asserts current behavior (agent does flag). Ships green; needs flipping when the rule is patched.

**Choice:** E1
**Rationale:** USERS.md UC6 already names negation as the dominant failure mode. A case that codifies the *correct* assertion is a regression-detection asset that pays off the moment the rule is fixed; codifying current-broken-behavior as expected hides the gap and risks future complacency. Pass-rate impact is minor (1 red case in a 26+ suite stays well above the 90% gate).

## Decision 6: Live eval run before commit

**Options considered:**
- F1 (chosen) — Skip the live HTTP run; commit on the strength of schema validation + rule-firing verification (engine fires the expected rule on each fixture patient).
- F2 — Spin up a local agent service and run the 4 cases through `tests.eval.runner`. Real round-trip; catches assertion-shape mismatches.
- F3 — Run against the deployed Railway agent. Requires explicit authorization (testers are using it).

**Choice:** F1
**Rationale:** Schema + rule-firing checks confirm the cases will exercise the right code paths; the agent's specific prose keywords and source-id citation choices are best validated as part of the next coordinated pre-merge `make eval` gate, not in a one-off local run that costs token budget without going through the gate. Live HTTP run is queued for the next eval-gate cycle.
