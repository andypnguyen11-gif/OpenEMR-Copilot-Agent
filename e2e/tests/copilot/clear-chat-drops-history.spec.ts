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
  test('clear-chat button drops session history', async ({ page }) => {
    // Use patient 102 (one condition) so the same "which one" probe
    // gives us a stable abstention signal without depending on patient
    // 101's two-condition path.
    await page.goto(CHAT_PATH);
    await selectPatient(page, PATIENTS.jamesOneCondition);

    // 1. Seed a turn so chat.js has a session_id to clear.
    const turn1 = await ask(page, "What are this patient's active problems?");
    const sessionId = turn1.response.session_id;
    expect(sessionId).toMatch(/^[0-9a-f]{32}$/);
    expect(turn1.response.abstention).toBeNull();

    // 2. Click "Clear chat" — same DELETE contract as patient switch.
    const del = await captureSessionDelete(page, async () => {
      await page.getByRole('button', { name: 'Clear chat' }).click();
    });
    expect(del.status).toBe(204);
    expect(del.url).toContain(`/api/agent/session/${sessionId}`);
    expect(del.url).toContain(`patient_id=${PATIENTS.jamesOneCondition}`);

    // 3. Same follow-up, same patient — but without prior memory the
    //    agent must not be able to resolve "which one".
    const turn2 = await ask(page, 'Which one was diagnosed first?');
    expect(turn2.status).toBe(200);
    expect(turn2.request.session_id).toBeUndefined();
    expect(turn2.response.session_id).not.toBe(sessionId);
    expect(turn2.response.abstention).not.toBeNull();
    expect(turn2.response.abstention?.state).toBe('VERIFICATION_FAILED');
  });
});
