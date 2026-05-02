# Copilot Session Store — Test Plan

End-to-end coverage for the multi-turn session contract added in PR9
(`feat/agent-service-pr9-multi-turn-sessions`). Tests exercise the
full stack: chat UI → PHP gateway → agent-service → Anthropic.

**Seed:** `tests/auth.setup.ts`  (admin login → storage state reused
by every test).

**Fixture patients** (hand-encoded in chat.php):
- `101` — Maria Lopez: T2DM (2019-04-12) + HTN (2018-11-03), two conditions.
- `102` — James Wright: T2DM (2017-06-22) only, one condition.

The "Which one was diagnosed first?" probe is deterministic on 101
(HTN is earlier) and always abstains on 102 (no antecedent). This is
the structural signal we use to assert the presence or absence of
multi-turn memory without text-matching LLM output.

---

## 1. Multi-turn continuity within a session

**Test:** `tests/copilot/multi-turn-continuity.spec.ts`

**Steps:**
1. Open `/interface/copilot/chat.php` (default patient 101).
2. Ask "What are this patient's active problems?".
3. Ask "Which one was diagnosed first?".

**Expected:**
- Turn 1 request body has no `session_id`; response mints a 32-char hex id.
- Turn 2 request body carries turn 1's id; response echoes the same id.
- Turn 2 response prose first item cites `Condition/p101-cond-2` (HTN,
  the earlier onset) — proves the agent had access to turn 1's
  context, not just the raw chart.

---

## 2. Patient switch drops session history

**Test:** `tests/copilot/patient-switch-drops-history.spec.ts`

**Steps:**
1. Open chat (patient 101).
2. Ask "What are this patient's active problems?" — seeds a session.
3. Switch the patient combobox to 102.
4. Ask "Which one was diagnosed first?" under the new patient.

**Expected:**
- Step 3 fires `DELETE /api/agent/session/{sessionId}?patient_id=101`
  → 204. The DELETE binds to the *outgoing* patient id (snapshot from
  before the change) so the JWT principal aligns with the session
  being torn down.
- Step 4 request body has no `session_id` (chat.js cleared local state).
- Step 4 response carries a fresh `session_id` (different from step 2's).
- Step 4 response is an abstention with `state: VERIFICATION_FAILED` —
  no antecedent to resolve "which one".

---

## 3. Clear chat drops session history

**Test:** `tests/copilot/clear-chat-drops-history.spec.ts`

**Steps:**
1. Open chat, switch to patient 102 (one condition baseline).
2. Ask "What are this patient's active problems?" — seeds a session.
3. Click "Clear chat".
4. Ask "Which one was diagnosed first?" with the same patient.

**Expected:** Same DELETE contract as scenario 2, except `patient_id`
in the DELETE query string is 102. Step 4 abstains on the same probe.

---

## 4. session_id round-trips between turns

**Test:** `tests/copilot/session-id-roundtrip.spec.ts`

**Steps:**
1. Open chat (patient 101).
2. Ask any question.
3. Ask any follow-up.

**Expected:**
- Turn 1 request has no `session_id`; response mints one.
- Turn 2 request body's `session_id` matches turn 1 response's
  `session_id`. Turn 2 response echoes the same id (server hit the
  same record, did not re-create).

---

## Out of scope (covered by unit tests, not e2e)

These are pinned in `agent-service/tests/unit/test_session_store.py`
and `tests/unit/test_orchestrator_slow.py`; we do not duplicate them
here:

- Cross-principal session_id replay returns empty history.
- TTL eviction at 30 minutes.
- Per-key lock under concurrent same-session POSTs.
- Composite-key miss returns 404 (not 401) from DELETE endpoint.
