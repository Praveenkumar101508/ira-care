import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test, expect, type Page } from '@playwright/test';
import { API, DASH, FAMILY, appSignIn, dashboardSignIn, info, login } from './helpers';

/**
 * Round 10 verification, continued (docs/progress/VERIFICATION_MATRIX.md):
 *  - C-10.c a section of the person's record that fails to load is named, and the rest of the
 *           record still shows (the visits request is made to fail in the browser);
 *  - C-23.b automated accessibility checks on the phone app: axe in the page of the phone app's web
 *           build (the same React Native components; not the native accessibility tree, which needs
 *           a device), the sign-in screen and each signed-in experience's home.
 */
const i = info();
const manager = login(i, 'coordinator');
const person = i.people[0];

test.describe.configure({ mode: 'serial' });

test('a section that fails to load is named, and the rest of the record still shows', async ({ page }) => {
  await dashboardSignIn(page, manager);
  await page.route(new RegExp(`/api/v1/care/people/${person.id}/visits`), (route) => route.abort('failed'));
  await page.goto(`${DASH}/people/${person.id}?tab=care`);
  const live = page.getByTestId('live');
  await expect(live).toContainText('Some sections could not be loaded (visits)', { timeout: 60_000 });
  // The rest is there: the person's name, the watch line and the check-ins read from the server.
  await expect(page.getByText(person.display_name).first()).toBeVisible();
  await expect(page.getByText(/Watch:/).first()).toBeVisible();
  // Negative control: with the visits request working again, nothing is named.
  await page.unroute(new RegExp(`/api/v1/care/people/${person.id}/visits`));
  await page.reload();
  await expect(page.getByTestId('live')).toContainText('Live: updated', { timeout: 60_000 });
  await expect(page.getByTestId('live')).not.toContainText('could not be loaded');
});

const AXE = readFileSync(process.env.AXE_PATH ?? join(__dirname, '..', 'dashboard', 'node_modules', 'axe-core', 'axe.min.js'), 'utf8');

async function seriousViolations(page: Page): Promise<string[]> {
  await page.addScriptTag({ content: AXE });
  return page.evaluate(async () => {
    const r = await (window as unknown as { axe: { run: (c: Document, o: object) => Promise<{ violations: { id: string; impact: string | null; nodes: { target: string[] }[] }[] }> } })
      .axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } });
    return r.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
      .map((v) => `${v.id} (${v.impact}) at ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join('; ')}`);
  });
}

test.describe('the phone app, in its web build', () => {
  test.use({ bypassCSP: true });

  test('axe finds no serious or critical violation on the sign-in screen and each experience', async ({ browser }) => {
    test.setTimeout(240_000);
    const report: Record<string, string[]> = {};
    const page = await browser.newPage();
    await page.goto(FAMILY); await page.waitForLoadState('networkidle'); await page.waitForTimeout(1_500);
    report['sign-in'] = await seriousViolations(page);
    await page.close();
    for (const key of ['family', 'wearer', 'coordinator']) {
      const p = await browser.newPage();
      await appSignIn(p, login(i, key));
      await expect(p.getByTestId('tab-home')).toBeVisible({ timeout: 60_000 });
      await p.waitForTimeout(2_000);
      report[`${key} home`] = await seriousViolations(p);
      await p.getByTestId('tab-settings').click(); await p.waitForTimeout(1_000);
      report[`${key} settings`] = await seriousViolations(p);
      await p.close();
    }
    console.log(JSON.stringify({ phone_axe: report, api: API }));
    expect(report).toEqual(Object.fromEntries(Object.keys(report).map((k) => [k, []])));
  });
});
