import { test as setup, expect } from '@playwright/test';

const STORAGE_STATE = '.auth/admin.json';

// Logs into OpenEMR once and persists the session cookie. Every test in
// the chromium project runs with this state, avoiding a 1-2s login per
// case. The CSRF token is rotated on each main.php load so tests still
// pull a fresh value via window.top.api_csrf_token_js — we only need
// the OpenEMR cookie here.
setup('authenticate as admin', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/OpenEMR Login/);
  await page.getByRole('textbox', { name: 'Username' }).fill('admin');
  await page.getByRole('textbox', { name: 'Password' }).fill('pass');
  await page.getByRole('button', { name: 'Login' }).click();
  await expect(page).toHaveTitle('OpenEMR');
  await page.context().storageState({ path: STORAGE_STATE });
});
