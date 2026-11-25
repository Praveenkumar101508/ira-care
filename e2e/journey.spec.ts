import { test, expect, type Page } from '@playwright/test';
import { Api, DASH, appSignIn, dashboardSignIn, info, login } from './helpers';

/**
 * The whole journey, in one browser session: the coordinator adds a person and makes a
 * pairing code; the watch (in the browser) pairs with that code on its face; the server asks
 * a check-in and the person answers; a heart rate outside the range opens an alert; the
 * family member says "I'm on my way" on the phone, arrives, and closes it with an outcome;
 * the dashboard shows every step. Nothing here is faked: the real care server, database,
 * worker and model answer.
 */
const i = info();
const coordinator = login(i, 'coordinator');
const family = login(i, 'family');

test.describe.configure({ mode: 'serial' });

let personId = '';
let pairingCode = '';
let watch: Page;

test('the coordinator adds a person and makes a pairing code', async ({ page }) => {
  await dashboardSignIn(page, coordinator);
  await page.getByPlaceholder('Full name').fill('Edith Marlow');
  await page.getByPlaceholder('Preferred name').fill('Edith');
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  const edith = page.getByRole('link', { name: 'Edith' }).first();
  await expect(edith).toBeVisible();
  await edith.click();
  await page.waitForURL(/\/people\//);
  personId = page.url().match(/people\/([0-9a-f-]+)/)![1];
  await page.goto(`${DASH}/people/${personId}?tab=health`);
  await page.getByRole('button', { name: 'Create a pairing code' }).click();
  const code = page.locator('code').first();
  await expect(code).toBeVisible();
  pairingCode = (await code.textContent())!.trim();
  expect(pairingCode.length).toBeGreaterThan(3);
});

test('the watch pairs with the code typed on its face and shows all well', async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 520 } });
  watch = await ctx.newPage();
  await watch.goto(`${DASH}/watch`); await watch.waitForLoadState('networkidle');
  await watch.getByLabel('Pairing code').fill(pairingCode);
  await watch.getByRole('button', { name: 'Start' }).click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/All well/, { timeout: 30_000 });
});

test('the server asks a check-in and the person answers from the watch', async ({ page }) => {
  const api = await Api.signIn(i, coordinator);
  await api.req('POST', `/api/v1/care/people/${personId}/checkins/ask-now`, { kind: 'wellbeing' });
  await expect(watch.getByTestId('watch-text')).not.toHaveText(/All well/, { timeout: 40_000 });
  const question = await watch.getByTestId('watch-text').textContent();
  expect(question).toMatch(/Edith|\?/);
  await watch.getByTestId('typed').fill('I am fine thank you, I had my breakfast');
  await watch.getByTestId('typed').press('Enter');
  await expect(watch.getByTestId('watch-text')).toHaveText(/All well|Thank|Good|glad|Lovely|fine/i, { timeout: 40_000 });
  await dashboardSignIn(page, coordinator);
  await page.goto(`${DASH}/people/${personId}?tab=timeline`);
  await expect(page.getByText(/wellbeing/).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/answered/).first()).toBeVisible();
});

test('a heart rate outside the range opens an alert the family answers on the phone', async ({ page, browser }) => {
  const api = await Api.signIn(i, coordinator);
  // The family member is in Edith's circle, called first, linked to their own account: since D-076 a
  // family member sees only the people whose circle their account is in. The seeded circles hold that link.
  let familyUserId: string | null = null;
  for (const p of i.people) {
    const { members } = await api.req<{ members: { display_name: string; user_id: string | null }[] }>('GET', `/api/v1/care/people/${p.id}/circle`);
    familyUserId = members.find((m) => m.display_name === family.name && m.user_id)?.user_id ?? null;
    if (familyUserId) break;
  }
  expect(familyUserId, 'the seeded family account is linked in a seeded circle').toBeTruthy();
  await api.req('POST', `/api/v1/care/people/${personId}/circle`, { display_name: family.name ?? 'Family', role: 'family', tier: 1, phone: '+447700900001', relationship: 'daughter', user_id: familyUserId, sms_enabled: true, call_enabled: true, push_enabled: true, email: family.email });
  const settings = await api.req<Record<string, unknown>>('GET', `/api/v1/care/people/${personId}/settings`);
  await api.req('PUT', `/api/v1/care/people/${personId}/settings`, { ...settings, heart_rate_min: 45, heart_rate_max: 130, quiet_start: '00:00', quiet_end: '00:00' });
  // The watch's own readings: three samples a minute apart at 160 bpm, sent by the watch page's device client.
  const presenter = await watch.context().newPage();
  await presenter.goto(`${DASH}/presenter`); await presenter.waitForLoadState('networkidle');
  await presenter.getByTestId('inject-heart').click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/are you OK/i, { timeout: 40_000 });
  await watch.getByTestId('help').click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/Help request received|Sending|received/i, { timeout: 30_000 });
  await presenter.close();

  const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage();
  await appSignIn(phone, family);
  const alertsTab = phone.getByTestId('tab-alerts');
  await alertsTab.click();
  const ack = phone.getByTestId(/^ack-/).first();
  await expect(ack).toBeVisible({ timeout: 30_000 });
  await ack.click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/on the way/i, { timeout: 30_000 });
  const arrived = phone.getByTestId(/^arrived-/).first();
  await expect(arrived).toBeVisible({ timeout: 30_000 });
  await arrived.click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/has arrived/i, { timeout: 30_000 });
  const resolve = phone.getByTestId(/^resolve-/).first();
  await expect(resolve).toBeVisible({ timeout: 30_000 });
  await resolve.click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/resolved|All well/i, { timeout: 30_000 });
  await phone.context().close();

  await dashboardSignIn(page, coordinator);
  await page.goto(`${DASH}/people/${personId}?tab=alerts`);
  await expect(page.getByText(/Heart rate outside the set range/).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/resolved/).first()).toBeVisible();
});

test('the coordinator plans a visit and the family member records its outcome', async ({ page }) => {
  const api = await Api.signIn(i, coordinator);
  const start = new Date(Date.now() + 60 * 60 * 1000);
  const visit = await api.req<{ id: string; tasks: { id: string }[] }>('POST', `/api/v1/care/people/${personId}/visits`, {
    planned_start: start.toISOString(), planned_end: new Date(start.getTime() + 3_600_000).toISOString(), carer_name: family.name ?? 'Family', tasks: ['Breakfast', 'Medication'] });
  const fam = await Api.signIn(i, family);
  await fam.req('POST', `/api/v1/care/visits/${visit.id}/arrive`);
  await expect(watch.getByTestId('watch-text').or(watch.locator('.watch-visit'))).toContainText(/here|All well/i, { timeout: 30_000 });
  await fam.req('POST', `/api/v1/care/visits/${visit.id}/depart`, { outcome: 'all_well', handover: 'Ate well, in good spirits.', tasks_done: [visit.tasks[0].id] });
  await dashboardSignIn(page, coordinator);
  await page.goto(`${DASH}/people/${personId}?tab=care`);
  await expect(page.getByText(/Ate well/).first()).toBeVisible({ timeout: 30_000 });
});
