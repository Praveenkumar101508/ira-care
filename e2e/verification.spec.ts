import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test, expect, type Page } from '@playwright/test';
import { DASH, dashboardSignIn, info, login } from './helpers';

/**
 * Round 10 verification in a real browser, against the running care server and the production
 * dashboard build (`next build`, `next start`), of behaviour the register held only on unit tests:
 *
 *  - J.h   the demonstration pages answer 404 in a production build;
 *  - G2.c  the dashboard says when monitoring has paused, and only then (E2E_EXPECT_MONITORING:
 *          "running" against a server whose care jobs run, "paused" against one started with no
 *          care jobs, IRA_WORKER_JOBS=none, once its last runs are older than the server's five minutes);
 *  - C-11.a a watch that has not synced for hours is shown as last known, and one that never said
 *          whether it is worn as unknown: never as "on the wrist" (the row is set as a silent watch
 *          leaves it, with E2E_ADMIN_DSN, and put back afterwards);
 *  - C-23.a axe-core, run in the page, finds no serious or critical accessibility violation on the
 *          sign-in page, the people list, a person's record, alerts and visits.
 *
 * Fictional people only (scripts/demo/seed_demo.py).
 */
const i = info();
const manager = login(i, 'coordinator');
const person = i.people[0];
const ADMIN_DSN = process.env.E2E_ADMIN_DSN ?? '';
const MONITORING = process.env.E2E_EXPECT_MONITORING ?? '';

test.describe.configure({ mode: 'serial' });

function sql(statement: string): string {
  return execFileSync('psql', [ADMIN_DSN, '-v', 'ON_ERROR_STOP=1', '-Atc', statement], { encoding: 'utf8' }).trim();
}

test('the demonstration pages answer 404 in a production build, and the real pages do not', async ({ request }) => {
  for (const path of ['/watch', '/presenter', '/simulator', '/presenter/anything']) {
    const r = await request.get(`${DASH}${path}`);
    expect(r.status(), path).toBe(404);
    expect(await r.text(), path).not.toContain('Presenter controls');
  }
  // Negative control: the pages a carer uses are served.
  for (const path of ['/login', '/']) expect((await request.get(`${DASH}${path}`)).status(), path).toBe(200);
});

test('the dashboard says when monitoring has paused, and only then', async ({ page }) => {
  test.skip(!MONITORING, 'set E2E_EXPECT_MONITORING to running or paused');
  await dashboardSignIn(page, manager);
  await page.goto(`${DASH}/`); await page.waitForLoadState('networkidle');
  const banner = page.getByTestId('monitoring-paused');
  if (MONITORING === 'paused') {
    await expect(banner).toBeVisible({ timeout: 90_000 });
    await expect(banner).toContainText('Check-ins and alerts are not being processed');
    // It is on the person's record too, where a carer acts.
    await page.goto(`${DASH}/people/${person.id}?tab=care`);
    await expect(page.getByTestId('monitoring-paused')).toBeVisible({ timeout: 60_000 });
  } else {
    await expect(page.getByText(person.display_name).first()).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(5_000);
    await expect(banner).toHaveCount(0);
  }
});

test('a silent watch is shown as last known, a watch that never said as unknown: never as on the wrist', async ({ page }) => {
  test.skip(!ADMIN_DSN, 'set E2E_ADMIN_DSN (the seed database, as its owner) to set the watch rows');
  const before = sql(`SELECT on_wrist::text || '|' || COALESCE(last_sync_at::text, '') FROM care_devices WHERE person_id = '${person.id}' ORDER BY created_at LIMIT 1`);
  expect(before, 'the seed pairs a watch with the first person').not.toBe('');
  const [wrist, synced] = before.split('|');
  try {
    await dashboardSignIn(page, manager);
    // Worn, but silent for three hours (the record treats over an hour as stale).
    sql(`UPDATE care_devices SET on_wrist = true, last_sync_at = NOW() - INTERVAL '3 hours' WHERE person_id = '${person.id}'`);
    await page.goto(`${DASH}/people/${person.id}?tab=care`);
    const watch = page.getByText(/Watch:/).first().locator('..');
    await expect(watch).toContainText('last known on the wrist', { timeout: 60_000 });
    await expect(watch).not.toContainText(/, on the wrist,/);

    // Never said whether it is worn.
    sql(`UPDATE care_devices SET on_wrist = NULL, last_sync_at = NOW() WHERE person_id = '${person.id}'`);
    await page.reload();
    await expect(page.getByText(/Watch:/).first().locator('..')).toContainText('wrist unknown', { timeout: 60_000 });
    await expect(page.getByText(/Watch:/).first().locator('..')).not.toContainText('on the wrist');

    // Negative control: worn and recently synced reads "on the wrist".
    sql(`UPDATE care_devices SET on_wrist = true, last_sync_at = NOW() WHERE person_id = '${person.id}'`);
    await page.reload();
    await expect(page.getByText(/Watch:/).first().locator('..')).toContainText(', on the wrist,', { timeout: 60_000 });
  } finally {
    sql(`UPDATE care_devices SET on_wrist = ${wrist === '' ? 'NULL' : wrist}, last_sync_at = ${synced ? `'${synced}'` : 'NULL'} WHERE person_id = '${person.id}'`);
  }
});

// axe-core comes from the dashboard's own development dependencies (its unit accessibility tests use it).
const AXE = readFileSync(process.env.AXE_PATH ?? join(__dirname, '..', 'dashboard', 'node_modules', 'axe-core', 'axe.min.js'), 'utf8');
type Violation = { id: string; impact: string | null; help: string; nodes: { target: string[] }[] };

async function axe(page: Page): Promise<Violation[]> {
  await page.addScriptTag({ content: AXE });
  const found = await page.evaluate(async () => {
    const r = await (window as unknown as { axe: { run: (c: Document, o: object) => Promise<{ violations: Violation[] }> } })
      .axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } });
    return r.violations;
  });
  return found.filter((v) => v.impact === 'serious' || v.impact === 'critical');
}

test.describe('accessibility in the browser', () => {
  // The page's own security policy refuses scripts without its nonce: the test context lets the
  // checker in. Nothing else about the page changes.
  test.use({ bypassCSP: true });

  test('axe finds no serious or critical violation on the sign-in page and the carer pages', async ({ page }) => {
    const report: Record<string, string[]> = {};
    await page.goto(`${DASH}/login`); await page.waitForLoadState('networkidle');
    report['/login'] = (await axe(page)).map((v) => `${v.id} (${v.impact}): ${v.help} at ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join('; ')}`);
    await dashboardSignIn(page, manager);
    for (const path of ['/', `/people/${person.id}?tab=care`, `/people/${person.id}?tab=health`, '/alerts', '/visits']) {
      await page.goto(`${DASH}${path}`); await page.waitForLoadState('networkidle'); await page.waitForTimeout(1_500);
      report[path] = (await axe(page)).map((v) => `${v.id} (${v.impact}): ${v.help} at ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join('; ')}`);
    }
    console.log(JSON.stringify({ axe: report }));
    expect(report).toEqual(Object.fromEntries(Object.keys(report).map((k) => [k, []])));
  });
});
