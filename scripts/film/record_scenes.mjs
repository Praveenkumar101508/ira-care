// Record the film's scenes from a running staging stack (scripts/staging/clean_clone_proof.sh --keep).
//
//   node scripts/film/record_scenes.mjs
//
// Expects IRA_CARE_API, DASHBOARD_URL, FAMILY_URL and IRA_DEMO_INFO (as the staging script
// exports them) and writes, for each scene, docs/film/raw/<scene>.webm (1080 pixels high: each
// page is laid out at two thirds of that and drawn at 1.5 times, with CSS zoom, because the
// video recorder ignores the device pixel ratio) and <scene>.json: when IRA spoke on the watch and
// what it said (the real words, caught from the watch page's own speech call), and the zooms.
// The presenter page drives the watch from a second tab whose video is discarded; recordings
// show only what a person would see. The SOS scene records the watch and Sravani's phone at
// the same time (<scene>.webm and <scene>-phone.webm) so the film can show both side by side.
// Zooms are done in the page itself (a CSS transform on the whole page), so the recording is
// as sharp zoomed as it is not.
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)('playwright');
import { readFileSync, writeFileSync, mkdirSync, renameSync, rmSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8010';
const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3010';
const FAMILY = process.env.FAMILY_URL ?? 'http://localhost:8091';
const OUT = process.env.FILM_RAW ?? 'docs/film/raw';
const ONLY = (process.env.FILM_SCENES ?? '').split(',').filter(Boolean);
const info = JSON.parse(readFileSync(process.env.IRA_DEMO_INFO ?? '.demo/demo.local.json', 'utf8'));
const coordinator = info.logins.find((l) => l.key === 'coordinator') ?? info.logins.find((l) => l.role === 'workspace_admin');
const family = info.logins.find((l) => l.key === 'family') ?? info.logins.find((l) => l.role === 'member');
const ada = info.people[0];
mkdirSync(OUT, { recursive: true });
/** Pages are laid out at these sizes and drawn 1.5 times larger: 1280 x 720 becomes 1920 x 1080. */
const ZOOM = 1.5;
const DESK = { width: 1280, height: 720 };
const WATCH = { width: 720, height: 720 };
const PHONE = { width: 390, height: 720 };
const px = (v) => ({ width: Math.round(v.width * ZOOM), height: Math.round(v.height * ZOOM) });
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

/** Catch every line the watch page speaks, with the time it was spoken. Runs in the page. */
const CATCH_SPEECH = () => {
  if (!location.pathname.startsWith('/watch')) return;
  window.__spoken = [];
  const synth = window.speechSynthesis;
  const fake = { speak: (u) => window.__spoken.push({ at: Date.now(), text: u.text }), cancel: () => {}, pause: () => {}, resume: () => {}, getVoices: () => (synth ? synth.getVoices() : []), speaking: false, pending: false };
  Object.defineProperty(window, 'speechSynthesis', { value: fake, configurable: true });
};

/** Draw the page at ZOOM times its layout size (the window is ZOOM times larger). Runs in the page. */
const DRAW_LARGER = (zoom) => {
  const apply = () => {
    if (!document.body || !document.head) { requestAnimationFrame(apply); return; }
    document.body.style.zoom = String(zoom);
    const css = document.createElement('style');
    css.textContent = `.watch-page { min-height: calc(100vh / ${zoom}) !important; }`;
    document.head.appendChild(css);
  };
  apply();
};

const browser = await chromium.launch();
const keep = [];
let marks = null;
let sceneStart = 0;
/** Note a moment in the scene's timeline (seconds from the start of its video). */
const mark = (what, extra = {}) => marks.push({ t: +((Date.now() - sceneStart) / 1000).toFixed(2), what, ...extra });

/** Zoom the page smoothly on an element so it fills about `fill` of the height, then hold. */
async function zoomOn(page, locator, { fill = 0.55, max = 2.4, ms = 900 } = {}) {
  const box = await locator.first().boundingBox().catch(() => null);
  if (!box) return;
  const vp = page.viewportSize();
  const s = Math.max(1.2, Math.min(max, (vp.height * fill) / box.height, (vp.width * 0.9) / box.width));
  await page.evaluate(([b, s, ms, vw, vh]) => {
    const el = document.documentElement;
    const cx = b.x + b.width / 2 + window.scrollX, cy = b.y + b.height / 2 + window.scrollY;
    el.style.transition = `transform ${ms}ms cubic-bezier(.4,0,.2,1)`;
    el.style.transformOrigin = `${cx}px ${cy}px`;
    el.style.transform = `translate(${vw / 2 - (b.x + b.width / 2)}px, ${vh / 2 - (b.y + b.height / 2)}px) scale(${s})`;
  }, [box, s, ms, vp.width, vp.height]);
  mark('zoom', { scale: +s.toFixed(2) });
  await pause(page, ms);
}
async function zoomOut(page, ms = 800) {
  await page.evaluate((ms) => { const el = document.documentElement; el.style.transition = `transform ${ms}ms cubic-bezier(.4,0,.2,1)`; el.style.transform = 'none'; }, ms);
  mark('unzoom');
  await pause(page, ms);
}

/** Record one scene: fn gets the page and context; the first page's video is kept, with its speech and marks. */
async function scene(name, opts, fn) {
  if (ONLY.length && !ONLY.includes(name)) return;
  const dir = join(OUT, `_${name}`); rmSync(dir, { recursive: true, force: true });
  const vp = opts.viewport ?? DESK;
  const context = await browser.newContext({ viewport: px(vp), isMobile: opts.isMobile, hasTouch: opts.isMobile, recordVideo: { dir, size: px(vp) } });
  await context.addInitScript(CATCH_SPEECH);
  await context.addInitScript(DRAW_LARGER, ZOOM);
  marks = [];
  const page = await context.newPage();
  sceneStart = Date.now();
  // A clean slate: the watch's own pairing lives in this browser profile.
  await page.goto(`${DASH}/watch`); await page.evaluate(() => window.localStorage.removeItem('ira_care_simulator'));
  const extra = [];
  try { await fn(page, context, extra); } catch (e) { console.error(`scene ${name} failed:`, e.message); }
  const spoken = await page.evaluate(() => window.__spoken ?? []).catch(() => []);
  const video = await page.video()?.path();
  await context.close();
  for (const x of extra) await x.close();
  const seconds = Math.round((Date.now() - sceneStart) / 1000);
  if (video && existsSync(video)) {
    renameSync(video, join(OUT, `${name}.webm`));
    const said = spoken.map((s) => ({ t: +((s.at - sceneStart) / 1000).toFixed(2), text: s.text }));
    writeFileSync(join(OUT, `${name}.json`), JSON.stringify({ scene: name, seconds, size: px(vp), said, marks }, null, 1));
    keep.push({ name, seconds, said: said.length });
    console.log('scene', name, seconds, 's;', said.length, 'lines spoken by IRA');
  }
  rmSync(dir, { recursive: true, force: true });
}
/** A second recorded page in the same scene (another device), started at the same moment. */
async function secondDevice(name, vp, isMobile, extra) {
  const dir = join(OUT, `_${name}`); rmSync(dir, { recursive: true, force: true });
  const ctx = await browser.newContext({ viewport: px(vp), isMobile, hasTouch: isMobile, recordVideo: { dir, size: px(vp) } });
  await ctx.addInitScript(DRAW_LARGER, ZOOM);
  const page = await ctx.newPage();
  const offset = (Date.now() - sceneStart) / 1000;
  extra.push({ close: async () => { const v = await page.video()?.path(); await ctx.close(); if (v && existsSync(v)) renameSync(v, join(OUT, `${name}.webm`)); writeFileSync(join(OUT, `${name}.offset`), String(offset.toFixed(2))); rmSync(dir, { recursive: true, force: true }); } });
  return page;
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
async function quietAlerts() {
  const open = await co('GET', '/api/v1/care/alerts?open_only=true&limit=100');
  for (const a of open.alerts ?? []) await co('POST', `/api/v1/care/alerts/${a.id}/resolve`, { resolution: 'Before the recording.', outcome: 'all_well' }).catch(() => null);
}
// Quiet: no open alerts from earlier runs, no quiet hours, a range the heart scene can leave.
await quietAlerts();
const settings = await co('GET', `/api/v1/care/people/${ada.id}/settings`);
await co('PUT', `/api/v1/care/people/${ada.id}/settings`, { ...settings, heart_rate_min: 45, heart_rate_max: 130, quiet_start: '00:00', quiet_end: '00:00' });
const watchText = (page) => page.getByTestId('watch-text');

// 03. The watch asks a check-in, composed by the model, and Ada answers; IRA replies.
await quietWatch();
await scene('03-watch-asks', { viewport: WATCH }, async (page, context) => {
  const presenter = await pairWatch(page, context);
  await pause(page, 2500);
  await co('POST', `/api/v1/care/people/${ada.id}/checkins/ask-now`, { kind: 'wellbeing' });
  await watchText(page).filter({ hasNotText: 'All well' }).waitFor({ timeout: 60000 }).catch(() => null);
  mark('question');
  await zoomOn(page, page.getByTestId('watch-face'), { fill: 0.9, max: 1.35 });
  await pause(page, 5000);
  await zoomOut(page);
  await page.getByTestId('typed').fill('I am well thank you, I slept nicely'); await pause(page, 1500);
  const asked = await watchText(page).textContent();
  await page.getByTestId('typed').press('Enter');
  await watchText(page).filter({ hasNotText: asked ?? '' }).waitFor({ timeout: 60000 }).catch(() => null);
  mark('answered');
  await pause(page, 3000);
  // Then Ada talks to IRA, and IRA answers back: the instant acknowledgement, then the model's words.
  const before = await page.evaluate(() => (window.__spoken ?? []).length);
  await page.getByTestId('typed').fill('I think I will sit in the garden this afternoon'); await pause(page, 1500);
  await page.getByTestId('typed').press('Enter'); mark('talk');
  await page.waitForFunction((n) => (window.__spoken ?? []).length >= n + 2, before, { timeout: 60000 }).catch(() => null);
  await pause(page, 7000);
  await presenter.close();
});
// 04. A heart rate outside the range: IRA asks first.
await quietWatch();
await scene('04-ask-first', { viewport: WATCH }, async (page, context) => {
  const presenter = await pairWatch(page, context);
  await pause(page, 2000);
  await presenter.getByTestId('inject-heart').click(); await page.bringToFront();
  await watchText(page).filter({ hasText: /are you OK/i }).waitFor({ timeout: 60000 }).catch(() => null);
  mark('ask-first');
  await zoomOn(page, watchText(page), { fill: 0.45 });
  await pause(page, 6000);
  await zoomOut(page);
  await page.getByTestId('ok').click(); mark('ok');
  await pause(page, 7000);
  await presenter.close();
});
// 05. Help from the wrist, reaching Sravani's phone; "I'm on my way" and "I've arrived" back on the wrist.
await quietWatch(); await quietAlerts();
await scene('05-sos', { viewport: WATCH }, async (page, context, extra) => {
  const presenter = await pairWatch(page, context);
  const phone = await secondDevice('05-sos-phone', PHONE, true, extra);
  await appSignIn(phone, family);
  await phone.getByTestId('tab-alerts').click().catch(() => null);
  await pause(page, 2000);
  await page.getByTestId('help').click(); mark('help');
  await watchText(page).filter({ hasText: /received/i }).waitFor({ timeout: 30000 }).catch(() => null);
  await zoomOn(page, watchText(page), { fill: 0.4 });
  await pause(page, 3000); await zoomOut(page);
  // the alert reaches the phone (the app refreshes the list; a pull is what a person would do)
  const ack = phone.getByTestId(/^ack-/).first();
  for (let i = 0; i < 20 && !(await ack.count()); i++) { await phone.getByTestId('tab-home').click().catch(() => null); await pause(phone, 800); await phone.getByTestId('tab-alerts').click().catch(() => null); await pause(phone, 1500); }
  mark('phone-alert');
  await zoomOn(phone, ack, { fill: 0.25, max: 1.6 });
  await pause(phone, 3000);
  await ack.click(); mark('on-my-way');
  await zoomOut(phone);
  await watchText(page).filter({ hasText: /on the way/i }).waitFor({ timeout: 30000 }).catch(() => null);
  mark('watch-on-the-way');
  await zoomOn(page, watchText(page), { fill: 0.4 });
  await pause(page, 5000); await zoomOut(page);
  const arrived = phone.getByTestId(/^arrived-/).first();
  if (await arrived.count()) { await arrived.click(); mark('arrived'); }
  await watchText(page).filter({ hasText: /arrived|here/i }).waitFor({ timeout: 30000 }).catch(() => null);
  await pause(page, 5000);
  const resolve = phone.getByTestId(/^resolve-/).first();
  if (await resolve.count()) { await resolve.click(); mark('resolved'); }
  await pause(page, 5000);
  await presenter.close();
});
// 06. The coordinator's dashboard.
await scene('06-dashboard', {}, async (page) => {
  await dashboardSignIn(page); await pause(page, 5000);
  await page.goto(`${DASH}/people/${ada.id}?tab=overview`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=health`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=alerts`); await pause(page, 6000);
  await page.goto(`${DASH}/people/${ada.id}?tab=timeline`); await pause(page, 6000);
});
// 07. A visit on the watch: the carer is expected, then arrives.
await quietWatch();
let visit = null;
await scene('07-visit', { viewport: WATCH }, async (page, context) => {
  const start = new Date(Date.now() + 20 * 60 * 1000);
  visit = await co('POST', `/api/v1/care/people/${ada.id}/visits`, { planned_start: start.toISOString(), planned_end: new Date(start.getTime() + 3600000).toISOString(), carer_name: coordinator.name ?? 'Carer', tasks: ['Breakfast', 'Medication', 'A walk'] });
  const presenter = await pairWatch(page, context);
  await page.locator('.watch-visit').waitFor({ timeout: 40000 }).catch(() => null);
  mark('expected');
  await zoomOn(page, page.getByTestId('watch-face'), { fill: 0.9, max: 1.35 });
  await pause(page, 4000);
  const fam = await api(family);
  await fam('POST', `/api/v1/care/visits/${visit.id}/arrive`); mark('carer-arrives');
  await watchText(page).filter({ hasText: /is here/i }).waitFor({ timeout: 40000 }).catch(() => null);
  mark('watch-here');
  await pause(page, 6000);
  await zoomOut(page);
  await pause(page, 2000);
  await presenter.close();
});
// 08. The morning report on the dashboard, after the handover.
await scene('08-morning-report', {}, async (page) => {
  if (visit) { const fam = await api(family); await fam('POST', `/api/v1/care/visits/${visit.id}/depart`, { outcome: 'all_well', handover: 'Ate well, in good spirits. Walked to the end of the road.', tasks_done: visit.tasks.slice(0, 2).map((t) => t.id) }).catch(() => null); }
  await dashboardSignIn(page);
  await page.goto(`${DASH}/people/${ada.id}?tab=care`); await pause(page, 5000);
  await page.goto(`${DASH}/people/${ada.id}?tab=reports`); await pause(page, 3000);
  await page.getByRole('button', { name: /daily report/i }).click(); mark('report');
  await pause(page, 4000);
  const report = page.locator('div.card:has(pre)').first();
  if (await report.count()) { await zoomOn(page, report, { fill: 0.8, max: 1.8 }); await pause(page, 9000); await zoomOut(page); }
  await pause(page, 2000);
});
// 09. Private by design: the IRA tab and consent.
await scene('09-private', {}, async (page) => {
  await dashboardSignIn(page);
  await page.goto(`${DASH}/people/${ada.id}?tab=ira`); await pause(page, 9000);
  await page.goto(`${DASH}/people/${ada.id}?tab=profile`); await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await pause(page, 8000);
});
await browser.close();
console.log(JSON.stringify(keep));
