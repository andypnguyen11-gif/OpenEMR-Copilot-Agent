// spec: specs/copilot-sessions.md
import { test, expect } from '@playwright/test';
import {
  ask,
  captureSessionDelete,
  CHAT_PATH,
  PATIENTS,
  selectPatient,
} from './_helpers';

test.describe('Copilot session store', () => {
  test('patient switch drops session history', async ({ page }) => {
    // 1. Open chat (patient 101 by default) and seed a session with one
    //    turn so there's something to evict.
    await page.goto(CHAT_PATH);
    const turn1 = await ask(page, "What are this patient's active problems?");
    const sessionId101 = turn1.response.session_id;
    expect(sessionId101).toMatch(/^[0-9a-f]{32}$/);

    // 2. Switch to patient 102. chat.js fire-and-forgets a DELETE bound
    //    to the OUTGOING patient_id (snapshot from before the change),
    //    not the incoming one — that's the contract that keeps the JWT
    //    principal aligned with the session being torn down.
    const del = await captureSessionDelete(page, async () => {
      await selectPatient(page, PATIENTS.jamesOneCondition);
    });
    expect(del.status).toBe(204);
    expect(del.url).toContain(`/api/agent/session/${sessionId101}`);
    expect(del.url).toContain(`patient_id=${PATIENTS.mariaTwoConditions}`);

    // 3. Same follow-up under the new patient. chat.js's session_id
    //    state must have cleared, so the request body has no
    //    session_id and the server mints a fresh one. Without prior
    //    history there is no antecedent for "which one", so the
    //    schema-violation retry path triggers an abstention rather
    //    than fabricating an answer.
    const turn2 = await ask(page, 'Which one was diagnosed first?');
    expect(turn2.status).toBe(200);
    expect(turn2.request.session_id).toBeUndefined();
    expect(turn2.request.patient_id).toBe(PATIENTS.jamesOneCondition);
    expect(turn2.response.session_id).not.toBe(sessionId101);
    expect(turn2.response.abstention).not.toBeNull();
    expect(turn2.response.abstention?.state).toBe('VERIFICATION_FAILED');
  });
});
