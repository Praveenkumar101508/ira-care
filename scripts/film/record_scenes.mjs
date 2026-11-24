// Record the film's scenes from a running staging stack (scripts/staging/clean_clone_proof.sh --keep).
//
//   node scripts/film/record_scenes.mjs
//
// Expects IRA_CARE_API, DASHBOARD_URL, FAMILY_URL and IRA_DEMO_INFO (as the staging script
// exports them) and writes one video per scene to docs/film/raw/<scene>.webm. The presenter
// page drives the watch from a second tab whose video is discarded; recordings show only
// what a person would see.
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)('playwright');
import { readFileSync, mkdirSync, renameSync, rmSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8010';
const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3010';
const FAMILY = process.env.FAMILY_URL ?? 'http://localhost:8091';
const OUT = process.env.FILM_RAW ?? 'docs/film/raw';
const info = JSON.parse(readFileSync(process.env.IRA_DEMO_INFO ?? '.demo/demo.local.json', 'utf8'));
const coordinator = info.logins.find((l) => l.key === 'coordinator') ?? info.logins.find((l) => l.role === 'workspace_admin');
const family = info.logins.find((l) => l.key === 'family') ?? info.logins.find((l) => l.role === 'member');
const ada = info.people[0];
mkdirSync(OUT, { recursive: true });
const SIZE = { width: 1280, height: 720 };
const pause = (p, ms) => p.waitForTimeout(ms);

async function api(login) {
  const form = new URLSearchParams({ email: login.email, password: login.password });
  const r = await fetch(`${API}/auth/cloud/login`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form.toString() });
  const t = await r.json();
  const headers = { Authorization: `Bearer ${t.access_token}`, 'X-Workspace-ID': info.workspace_id, 'Content-Type': 'application/json' };
  return async (method, path, body) => { const x = await fetch(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }); if (!x.ok) throw new Error(`${method} ${path} ${x.status} ${await x.text()}`); return x.status === 204 ? null : x.json(); };
}
async function dashboardSignIn(page) {
  await page.goto(`${DASH}/login`); await page.waitForLoadState('networkidle');
  await page.getByText('Advanced').click();
  for (let a = 0; a < 12; a++) { await page.getByLabel('Care server').fill(API); await page.getByLabel('Username').fill(coordinator.email); await page.getByLabel('Password').fill(coordinator.password); if (!(await page.getByRole('button', { name: /sign in/i }).isDisabled())) break; await pause(page, 500); }
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL((u) => !u.pathname.includes('login'), { timeout: 30000 });
}
async function appSignIn(page, login) {
  await page.goto(FAMILY); await page.waitForLoadState('networkidle'); await pause(page, 1500);
  await page.getByTestId('advanced').click();
  for (let a = 0; a < 12; a++) { await page.getByTestId('server').fill(API); await page.getByTestId('username').fill(login.email); await page.getByTestId('password').fill(login.password); if ((await page.getByTestId('sign-in').getAttribute('aria-disabled')) !== 'true') break; await pause(page, 500); }
  await page.getByTestId('sign-in').click(); await pause(page, 3000);
}

const browser = await chromium.launch();
const keep = [];
/** Record one scene: fn gets a context whose pages are recorded; the first page's video is kept. */
async function scene(name, opts, fn) {
  const dir = join(OUT, `_${name}`); rmSync(dir, { recursive: true, force: true });
  const context = await browser.newContext({ viewport: opts.viewport ?? SIZE, deviceScaleFactor: 1, isMobile: opts.isMobile, hasTouch: opts.isMobile, recordVideo: { dir, size: opts.viewport ?? SIZE } });
  const page = await context.newPage();
  // A clean slate: the watch's own pairing lives in this browser profile.
  await page.goto(`${DASH}/watch`); await page.evaluate(() => window.localStorage.removeItem('ira_care_simulator'));
  const start = Date.now();
  try { await fn(page, context); } catch (e) { console.error(`scene ${name} failed:`, e.message); }
  const video = await page.video()?.path();
  await context.close();
  const seconds = Math.round((Date.now() - start) / 1000);
  if (video && existsSync(video)) { renameSync(video, join(OUT, `${name}.webm`)); keep.push({ name, seconds }); console.log('scene', name, seconds, 's'); }
  rmSync(dir, { recursive: true, force: true });
}
async function pairWatch(page, context) {
  await page.goto(`${DASH}/watch`); await page.waitForLoadState('networkidle');
  const presenter = await context.newPage();
  await presenter.goto(`${DASH}/presenter`); await presenter.waitForLoadState('networkidle');
  for (let a = 0; a < 12; a++) { await presenter.getByTestId('token').fill(ada.device_token); await pause(page, 800); if ((await page.getByTestId('watch-face').textContent())?.includes('All well')) break; }
  await page.bringToFront();
  return presenter;
}
const co = await api(coordinator);
const deviceHeaders = { 'X-Device-Token': ada.device_token, 'Content-Type': 'application/json' };
/** The watch's own API, as the watch calls it. */
const dev = async (method, path, body) => { const r = await fetch(`${API}${path}`, { method, headers: deviceHeaders, body: body === undefined ? undefined : JSON.stringify(body) }); return r.ok ? r.json() : null; };
/** A clean face before a watch scene: no question pending, and no SOS closed in the last ten minutes (the face would say so). */
async function quietWatch() {
  for (let i = 0; i < 5; i++) { const d = await dev('GET', '/api/v1/care/devices/me/checkins/due'); if (!d?.checkin) break; await dev('POST', `/api/v1/care/devices/me/checkins/${d.checkin.id}/answer`, { text: 'I am fine, thank you', via: 'voice' }); }
  for (let i = 0; i < 70; i++) { const s = await dev('GET', '/api/v1/care/devices/me/sos'); if (!s || s.state === 'idle') return; await new Promise((r) => setTimeout(r, 10000)); }
}
// Quiet: no open alerts from earlier runs.
const open = await co('GET', '/api/v1/care/alerts?open_only=true&limit=100');
for (const a of open.alerts ?? []) await co('POST', `/api/v1/care/alerts/${a.id}/resolve`, { resolution: 'Before the recording.', outcome: 'all_well' }).catch(() => null);
const settings = await co('GET', `/api/v1/care/people/${ada.id}/settings`);
await co('PUT', `/api/v1/care/people/${ada.id}/settings`, { ...settings, heart_rate_min: 45, heart_rate_max: 130, quiet_start: '00:00', quiet_end: '00:00' });

// 2. The watch asks a check-in and Ada answers.
await quietWatch();
await scene('02-watch-asks', { viewport: { width: 720, height: 720 } }, async (page, context) => {
  const presenter = await pairWatch(page, context);
  await pause(page, 2500);
  await co('POST', `/api/v1/care/people/${ada.id}/checkins/ask-now`, { kind: 'wellbeing' });
  await page.getByTestId('watch-text').filter({ hasNotText: 'All well' }).waitFor({ timeout: 40000 }).catch(() => null);
  await pause(page, 5000);
  await page.getByTestId('typed').fill('I am well thank you, I slept nicely'); await pause(page, 1500);
  const asked = await page.getByTestId('watch-text').textContent();
  await page.getByTestId('typed').press('Enter');
  await page.getByTestId('watch-text').filter({ hasNotText: asked ?? '' }).waitFor({ timeout: 45000 }).catch(() => null);
  await pause(page, 6000);
  await presenter.close();
});
// 3. A heart rate outside the range: IRA asks first.
await quietWatch();
await scene('03-heart-rate', { viewport: { width: 720, height: 720 } }, async (page, context) => {
  const presenter = await pairWatch(page, context);
  await pause(page, 2000);
  await presenter.getByTestId('inject-heart').click(); await page.bringToFront();
  await page.getByTestId('watch-text').filter({ hasText: /are you OK/i }).waitFor({ timeout: 40000 }).catch(() => null);
  await pause(page, 6000);
  await page.getByTestId('ok').click(); await pause(page, 6000);
  await presenter.close();
});
// 4. Help from the wrist, and what the watch says afterwards.
await quietWatch();
let helpAlertId = null;
await scene('04-help', { viewport: { width: 720, height: 720 } }, async (page, context) => {
  const presenter = await pairWatch(page, context);
  await pause(page, 2000);
  await page.getByTestId('help').click();
  await page.getByTestId('watch-text').filter({ hasText: /received/i }).waitFor({ timeout: 30000 }).catch(() => null);
  await pause(page, 6000);
  const fam = await api(family);
  const alerts = await fam('GET', '/api/v1/care/alerts?open_only=true&limit=20');
  helpAlertId = alerts.alerts?.[0]?.id ?? null;
  if (helpAlertId) { await fam('POST', `/api/v1/care/alerts/${helpAlertId}/acknowledge`, { note: '', kind: 'on_my_way' }); }
  await page.getByTestId('watch-text').filter({ hasText: /on the way/i }).waitFor({ timeout: 30000 }).catch(() => null);
  await pause(page, 7000);
  await presenter.close();
});
// 5. The family's phone: the alert, arrived, the outcome.
await scene('05-family-phone', { viewport: { width: 390, height: 720 }, isMobile: true }, async (page) => {
  await appSignIn(page, family);
  await pause(page, 3000);
  await page.getByTestId('tab-alerts').click(); await pause(page, 4000);
  const arrived = page.getByTestId(/^arrived-/).first();
  if (await arrived.count()) { await arrived.click(); await pause(page, 4000); }
  const resolve = page.getByTestId(/^resolve-/).first();
  if (await resolve.count()) { await resolve.click(); await pause(page, 5000); }
  await page.getByTestId('tab-home').click(); await pause(page, 4000);
});
// 6. The coordinator's dashboard.
await scene('06-dashboard', {}, async (page) => {
  await dashboardSignIn(page); await pause(page, 5000);
  await page.goto(`${DASH}/people/${ada.id}?tab=overview`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=health`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=alerts`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=timeline`); await pause(page, 6000);
});
// 7. A visit: planned, arrived, handed over.
await scene('07-visit', {}, async (page) => {
  const start = new Date(Date.now() + 30 * 60 * 1000);
  const visit = await co('POST', `/api/v1/care/people/${ada.id}/visits`, { planned_start: start.toISOString(), planned_end: new Date(start.getTime() + 3600000).toISOString(), carer_name: coordinator.name ?? 'Carer', tasks: ['Breakfast', 'Medication', 'A walk'] });
  await dashboardSignIn(page);
  await page.goto(`${DASH}/people/${ada.id}?tab=care`); await pause(page, 6000);
  const fam = await api(family);
  await fam('POST', `/api/v1/care/visits/${visit.id}/arrive`); await pause(page, 1000);
  await page.reload(); await pause(page, 6000);
  await fam('POST', `/api/v1/care/visits/${visit.id}/depart`, { outcome: 'all_well', handover: 'Ate well, in good spirits. Walked to the end of the road.', tasks_done: visit.tasks.slice(0, 2).map((t) => t.id) });
  await page.reload(); await pause(page, 8000);
});
// 8. Private by design: the IRA tab and consent.
await scene('08-private', {}, async (page) => {
  await dashboardSignIn(page);
  await page.goto(`${DASH}/people/${ada.id}?tab=ira`); await pause(page, 9000);
  await page.goto(`${DASH}/people/${ada.id}?tab=profile`); await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await pause(page, 9000);
});
await browser.close();
console.log(JSON.stringify(keep));
