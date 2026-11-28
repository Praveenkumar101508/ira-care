import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { Api, FAMILY, appSignIn, info, login } from './helpers';

/**
 * Round 10 verification of the family phone app (its own web build, `npx expo export --platform web`,
 * the same code as the phone) in a real browser against the running care server, for behaviour the
 * register held only on unit tests:
 *
 *  - C-11.b the watch card never reads as worn when the watch has gone silent or never said: a watch
 *          worn but silent for three hours is "last known on the wrist", one that never said is
 *          "wrist unknown" (the rows set as a silent watch leaves them, with E2E_ADMIN_DSN, and put back);
 *  - D2.c  a reminder set for the person reaches the wearer's home and "Done" is recorded on the server;
 *  - D2.d  a visit planned by the care team reaches the wearer's home as the next visit, with the carer's name;
 *  - D2.b  the appearance the family member chooses on one device is the one their other device shows;
 *  - C-16.a with the server's access tokens lasting one minute (ACCESS_TOKEN_EXPIRE_MINUTES=1,
 *          E2E_ACCESS_TOKEN_SECONDS=60), the phone renews an expired sign-in with its refresh token and
 *          the family member stays signed in, with no "sign-in has expired" screen.
 *
 * Fictional people only (scripts/demo/seed_demo.py). FAMILY_URL is where the web build is served.
 */
const i = info();
const manager = login(i, 'coordinator');
const family = login(i, 'family');
const wearer = login(i, 'wearer');
const person = i.people[0];
const ADMIN_DSN = process.env.E2E_ADMIN_DSN ?? '';

test.describe.configure({ mode: 'serial' });

function sql(statement: string): string {
  return execFileSync('psql', [ADMIN_DSN, '-v', 'ON_ERROR_STOP=1', '-Atc', statement], { encoding: 'utf8' }).trim();
}

test('the phone never shows a silent or unknown watch as worn', async ({ page }) => {
  test.skip(!ADMIN_DSN, 'set E2E_ADMIN_DSN (the seed database, as its owner)');
  const before = sql(`SELECT on_wrist::text || '|' || COALESCE(last_sync_at::text, '') FROM care_devices WHERE person_id = '${person.id}' AND status = 'paired' ORDER BY created_at DESC LIMIT 1`);
  expect(before, 'a paired watch').not.toBe('');
  const [wrist, synced] = before.split('|');
  const set = (onWrist: string, ago: string) => sql(`UPDATE care_devices SET on_wrist = ${onWrist}, last_sync_at = NOW() - INTERVAL '${ago}' WHERE person_id = '${person.id}' AND status = 'paired'`);
  try {
    set('true', '3 hours');
    await appSignIn(page, family);
    const card = page.getByTestId('card-watch');
    await expect(card).toContainText('last known on the wrist', { timeout: 60_000 });

    set('NULL', '0 seconds');
    // No reload: the web build keeps no sign-in across a page load; the app's own 30 s refresh brings it in.
    await expect(page.getByTestId('card-watch')).toContainText('wrist unknown', { timeout: 75_000 });
    await expect(page.getByTestId('card-watch')).not.toContainText('on the wrist');

    // Negative control: worn and just synced reads "on the wrist", not "last known".
    set('true', '0 seconds');
    // No reload: the web build keeps no sign-in across a page load; the app's own 30 s refresh brings it in.
    await expect(page.getByTestId('card-watch')).toContainText('on the wrist', { timeout: 75_000 });
    await expect(page.getByTestId('card-watch')).not.toContainText('last known');
  } finally {
    sql(`UPDATE care_devices SET on_wrist = ${wrist === '' ? 'NULL' : wrist}, last_sync_at = ${synced ? `'${synced}'` : 'NULL'} WHERE person_id = '${person.id}' AND status = 'paired'`);
  }
});

test('a visit and a reminder set by the care team reach the wearer, and Done is recorded', async ({ page }) => {
  const api = await Api.signIn(i, manager);
  const marker = `Water the roses ${Date.now() % 100000}`;
  const start = new Date(Date.now() + 2 * 3_600_000);
  start.setUTCSeconds(0, 0);
  const visit = await api.req<{ id: string }>('POST', `/api/v1/care/people/${person.id}/visits`, {
    planned_start: start.toISOString(), planned_end: new Date(start.getTime() + 3_600_000).toISOString(),
    carer_name: 'Verification Carer', purpose: 'Round 10 check', tasks: [],
  });
  const reminder = await api.req<{ id: string }>('POST', `/api/v1/care/people/${person.id}/reminders`, {
    text: marker, due_at: new Date(Date.now() + 30 * 60_000).toISOString(), kind: 'reminder',
  });
  try {
    await appSignIn(page, wearer);
    const visits = page.getByTestId('card-next-visit');
    await expect(visits).toBeVisible({ timeout: 60_000 });
    // The seed plans no visit in the next day, so the next visit is the one just planned.
    await expect(visits).toContainText(`Verification Carer is coming at ${start.toISOString().slice(11, 16)}.`, { timeout: 60_000 });
    const reminders = page.getByTestId('card-reminders');
    await expect(reminders).toContainText(marker, { timeout: 60_000 });
    await page.getByLabel(`Done: ${marker}`).click();
    await expect.poll(async () => {
      const all = await api.req<{ reminders: { id: string; status: string }[] }>('GET', `/api/v1/care/people/${person.id}/reminders?open_only=false&limit=50`);
      return all.reminders.find((r) => r.id === reminder.id)?.status;
    }, { timeout: 30_000 }).toBe('confirmed');
    await expect(reminders).not.toContainText(marker, { timeout: 60_000 });   // done, so no longer open on the wrist's list
  } finally {
    await api.req('POST', `/api/v1/care/visits/${visit.id}/cancel`, {}).catch(() => undefined);
  }
});

test('the appearance chosen on one phone is the one the same person sees on another', async ({ browser }) => {
  const first = await browser.newPage();
  const second = await browser.newPage();
  const mine = await Api.signIn(i, family);
  const mode = async () => (await mine.req<{ appearance: { mode?: string } }>('GET', '/api/v1/care/me/preferences')).appearance.mode;
  // How dark the tab bar is drawn: 0 is black, 1 is white (relative luminance of its background).
  const luminance = (page: typeof first) => page.getByTestId('tab-settings').evaluate((el) => {
    let n: Element | null = el;
    while (n) {
      const m = getComputedStyle(n).backgroundColor.match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?\)/);
      if (m && (m[4] === undefined || Number(m[4]) > 0)) {
        const [r, g, b] = [m[1], m[2], m[3]].map((x) => { const c = Number(x) / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      }
      n = n.parentElement;
    }
    return 1;
  });
  try {
    await appSignIn(first, family);
    const lightBefore = await luminance(first);
    await first.getByTestId('tab-settings').click();
    await first.getByTestId('settings-appearance').click();
    await first.getByTestId('mode-dark').click();
    await expect.poll(mode, { timeout: 30_000 }).toBe('dark');   // kept on the server, for every device
    expect(await luminance(first), 'the first phone turned dark').toBeLessThan(0.2);

    // Another device: a fresh browser profile, signed in as the same person.
    await appSignIn(second, family);
    await expect.poll(() => luminance(second), { timeout: 45_000 }).toBeLessThan(0.2);
    expect(lightBefore, 'and it was light before the change').toBeGreaterThan(0.5);
  } finally {
    // Back to following the phone, from the first device.
    await first.getByTestId('mode-system').click().catch(() => undefined);
    await expect.poll(mode, { timeout: 30_000 }).not.toBe('dark').catch(() => undefined);
    await first.close(); await second.close();
  }
});

test('the phone renews an expired sign-in instead of signing the family member out', async ({ page }) => {
  const seconds = Number(process.env.E2E_ACCESS_TOKEN_SECONDS ?? '0');
  test.skip(!seconds, 'start the server with ACCESS_TOKEN_EXPIRE_MINUTES=1 and set E2E_ACCESS_TOKEN_SECONDS=60');
  test.setTimeout((seconds + 180) * 1000);
  const renewals: number[] = [];
  const refused: number[] = [];
  page.on('response', (r) => {
    if (r.url().endsWith('/auth/refresh')) renewals.push(r.status());
    else if (r.url().includes('/api/v1/care/') && r.status() === 401) refused.push(r.status());
  });
  await appSignIn(page, family);
  await expect(page.getByTestId('card-watch')).toBeVisible({ timeout: 60_000 });
  // Past the token's life, and two of the app's 30-second refreshes after it.
  await page.waitForTimeout((seconds + 75) * 1000);
  expect(refused.length, 'the expired token was refused by the server at least once').toBeGreaterThan(0);
  expect(renewals, 'renewed with the refresh token').toContain(200);
  await expect(page.getByTestId('card-watch')).toBeVisible();
  await expect(page.getByTestId('sign-in')).toHaveCount(0);
  await expect(page.getByTestId('signed-out-notice')).toHaveCount(0);
});

test('the phone build under test is the one served at FAMILY_URL', async ({ request }) => {
  expect((await request.get(FAMILY)).status()).toBe(200);
});
