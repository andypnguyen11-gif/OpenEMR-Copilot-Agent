// spec: specs/copilot-sessions.md
import { test, expect } from '@playwright/test';
import { ask, CHAT_PATH } from './_helpers';

test.describe('Copilot session store', () => {
  test('session_id round-trips between turns', async ({ page }) => {
    await page.goto(CHAT_PATH);

    // Turn 1: client sends no session_id (fresh chat); server mints one
    // and returns it as the canonical id for the session.
    const turn1 = await ask(page, "What are this patient's active problems?");
    expect(turn1.status).toBe(200);
    expect(turn1.request.session_id).toBeUndefined();
    expect(turn1.response.session_id).toMatch(/^[0-9a-f]{32}$/);

    // Turn 2: client must echo turn 1's server id back. The server
    // returns the same id (proof the same record was hit, not
    // re-created). Without this round-trip, every turn would be a
    // fresh session and the agent would have no continuity.
    const turn2 = await ask(page, 'Tell me more about the first one.');
    expect(turn2.status).toBe(200);
    expect(turn2.request.session_id).toBe(turn1.response.session_id);
    expect(turn2.response.session_id).toBe(turn1.response.session_id);
  });
});
