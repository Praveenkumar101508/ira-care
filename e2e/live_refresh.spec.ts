import { test, expect } from '@playwright/test';
import { API, Api, DASH, dashboardSignIn, info, login } from './helpers';

/**
 * The person record keeps itself up to date (register C-10) and the care team's search works,
 * in a real browser against the real server (requirements C-10.d, D3.g):
 *  - a visit planned elsewhere (here, through the API) appears on the open record without a reload;
 *  - when the server cannot be reached the record says so plainly and says what it last saw;
 *  - when it can again, the record recovers on its own and says it is live;
 *  - the workspace search finds the person and links to their record.
 */
const i = info();
const manager = login(i, 'coordinator');

test.describe.configure({ mode: 'serial' });

test('the open record picks up a new visit, warns when cut off, and recovers on its own', async ({ page }) => {
  const person = i.people[0];
  await dashboardSignIn(page, manager);
  await page.goto(`${DASH}/people/${person.id}?tab=care`);
  await expect(page.getByTestId('live')).toContainText('Live: updated', { timeout: 30_000 });

  const marker = `Live refresh check ${Date.now()}`;
  const api = await Api.signIn(i, manager);
  const start = new Date(Date.now() + 3 * 3_600_000);
  await api.req('POST', `/api/v1/care/people/${person.id}/visits`, {
    planned_start: start.toISOString(), planned_end: new Date(start.getTime() + 3_600_000).toISOString(),
    carer_name: 'E2E carer', purpose: 'Live refresh check', tasks: [marker],
  });
  // No reload: the record's own 30-second refresh must bring it in.
  await expect(page.getByText(marker)).toBeVisible({ timeout: 75_000 });

  // Cut the browser off from the care server; the record must say so, and keep what it had.
  await page.route(`${API}/**`, (route) => route.abort('internetdisconnected'));
  await expect(page.getByTestId('live')).toContainText(/Connection lost|Not connected/, { timeout: 75_000 });
  await expect(page.getByText(marker)).toBeVisible();

  // Connection back: live again, with nobody touching the page.
  await page.unroute(`${API}/**`);
  await expect(page.getByTestId('live')).toContainText('Live: updated', { timeout: 75_000 });
});

test('the care team search finds the person and leads to their record', async ({ page }) => {
  const person = i.people[0];
  await dashboardSignIn(page, manager);
  await page.goto(`${DASH}/search`);
  const q = person.display_name.split(' ')[0];
  await page.getByLabel('Search for').fill(q);
  await page.getByRole('button', { name: 'Search' }).click();
  // Results name the person as the team does: the preferred name when there is one.
  const link = page.getByTestId('search-results').getByRole('link', { name: new RegExp(`^${q}`) }).first();
  await expect(link).toBeVisible({ timeout: 30_000 });
  await link.click();
  await page.waitForURL((u) => u.pathname === `/people/${person.id}`);
  await expect(page.getByTestId('live')).toContainText('Live: updated', { timeout: 30_000 });
});
