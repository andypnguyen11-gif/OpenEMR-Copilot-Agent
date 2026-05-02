import type { Page, Response } from '@playwright/test';

export const CHAT_PATH = '/interface/copilot/chat.php';
export const QUERY_URL_RE = /\/apis\/default\/api\/agent\/query$/;
export const SESSION_DELETE_URL_RE = /\/apis\/default\/api\/agent\/session\//;

// Patient ids hand-encoded into the M3 fixture set (chat.php's <select>).
// 101 has two conditions (T2DM 2019-04-12 + HTN 2018-11-03) so the
// "which one was first" follow-up has a deterministic answer; 102 has
// only T2DM so the same follow-up should abstain.
export const PATIENTS = {
  mariaTwoConditions: '101',
  jamesOneCondition: '102',
} as const;

export type QueryRequestBody = {
  patient_id: string;
  query: string;
  session_id?: string;
};

export type AgentResponseBody = {
  cards: Array<{ title: string; kind: string; source_ids: string[] }>;
  prose: Array<{
    text: string;
    source_id: string;
    source_field: string;
    expected_value: string;
  }>;
  tool_results: Array<unknown>;
  abstention: { state: string; reason: string } | null;
  session_id: string;
};

export type ChatTurn = {
  request: QueryRequestBody;
  response: AgentResponseBody;
  status: number;
};

// Send a message in the chat surface and return the parsed request/
// response for the matching POST /api/agent/query. Resolves only after
// the wire round-trip — handles the chat.js submit-disable + LLM
// latency without caller-side sleeps.
export async function ask(page: Page, question: string): Promise<ChatTurn> {
  const responsePromise: Promise<Response> = page.waitForResponse(
    (r) => QUERY_URL_RE.test(r.url()) && r.request().method() === 'POST',
    { timeout: 90_000 },
  );
  await page.locator('[data-copilot-input]').fill(question);
  await page.locator('[data-copilot-submit]').click();
  const response = await responsePromise;
  const request = response.request();
  const requestBody = (await request.postDataJSON()) as QueryRequestBody;
  const responseBody = (await response.json()) as AgentResponseBody;
  return {
    request: requestBody,
    response: responseBody,
    status: response.status(),
  };
}

// Drive the patient combobox; the chat.js change-handler fires the
// fire-and-forget DELETE for the outgoing session. Caller can
// optionally await captureSessionDelete to assert the wire shape.
export async function selectPatient(page: Page, patientId: string): Promise<void> {
  await page.locator('[data-copilot-patient]').selectOption(patientId);
}

// Capture the next DELETE /api/agent/session/{id}?patient_id=... that
// fires after `trigger` runs. chat.js fires-and-forgets it from a
// click-handler, so we set up the listener before triggering.
export async function captureSessionDelete(
  page: Page,
  trigger: () => Promise<void>,
): Promise<{ url: string; status: number }> {
  const deletePromise: Promise<Response> = page.waitForResponse(
    (r) => SESSION_DELETE_URL_RE.test(r.url()) && r.request().method() === 'DELETE',
    { timeout: 10_000 },
  );
  await trigger();
  const response = await deletePromise;
  return { url: response.url(), status: response.status() };
}
