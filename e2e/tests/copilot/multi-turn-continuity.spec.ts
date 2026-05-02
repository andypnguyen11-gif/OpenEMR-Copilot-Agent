// spec: specs/copilot-sessions.md
import { test, expect } from '@playwright/test';
import { ask, CHAT_PATH, PATIENTS } from './_helpers';

test.describe('Copilot session store', () => {
  test('multi-turn continuity within a session', async ({ page }) => {
    // 1. Open the chat surface; default-selected patient is 101.
    await page.goto(CHAT_PATH);

    // 2. First turn: "What are this patient's active problems?"
    const turn1 = await ask(page, "What are this patient's active problems?");
    expect(turn1.status).toBe(200);
    expect(turn1.request.session_id).toBeUndefined();
    expect(turn1.response.session_id).toMatch(/^[0-9a-f]{32}$/);
    expect(turn1.response.abstention).toBeNull();
    // Two conditions, both surfaced.
    const t1Sources = turn1.response.cards[0]?.source_ids ?? [];
    expect(t1Sources).toEqual(
      expect.arrayContaining(['Condition/p101-cond-1', 'Condition/p101-cond-2']),
    );

    // 3. Follow-up: "which one" — only resolvable with multi-turn memory.
    const turn2 = await ask(page, 'Which one was diagnosed first?');
    expect(turn2.status).toBe(200);
    expect(turn2.request.session_id).toBe(turn1.response.session_id);
    expect(turn2.response.session_id).toBe(turn1.response.session_id);
    expect(turn2.response.abstention).toBeNull();
    // p101-cond-2 (Essential hypertension, onset 2018-11-03) is earlier
    // than p101-cond-1 (T2DM, 2019-04-12). Assert the first prose claim
    // cites cond-2 — stable structural assertion, no text-matching.
    expect(turn2.response.prose[0]?.source_id).toBe('Condition/p101-cond-2');
    expect(turn2.response.prose[0]?.source_field).toBe('onset_date');
    expect(turn2.response.prose[0]?.expected_value).toBe('2018-11-03');
    // Patient id is bound by JWT, never spoofable from client body.
    expect(turn2.request.patient_id).toBe(PATIENTS.mariaTwoConditions);
  });
});
