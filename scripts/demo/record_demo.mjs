// Record the IRA CARE demo with Playwright: screenshots and a video of the
// browser watch, the carer dashboard and the family app on the web. The
// presenter's controls (/presenter) run in a second tab whose video is thrown
// away, so the recording shows only what a person would see.
//
//   node scripts/demo/record_demo.mjs
//
// Expects: the demo API (IRA_CARE_API, default http://127.0.0.1:8000), the
// dashboard (DASHBOARD_URL, default http://localhost:3000) and the family app's
// web build (FAMILY_URL, default http://localhost:8081) to be running, and
// .demo/demo.local.json from seed_demo.py. Writes to docs/demo/ (screenshots)
// and docs/demo/raw/ (the video, compressed afterwards by record_demo.sh).
import { createRequire } from 'node:module';
// Playwright is resolved the CommonJS way so a machine-wide install (NODE_PATH) is found too.
const { chromium } = createRequire(import.meta.url)('playwright');
import { readFileSync, mkdirSync, renameSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8000';
const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3000';
const FAMILY = process.env.FAMILY_URL ?? 'http://localhost:8081';
const OUT = process.env.DEMO_OUT ?? 'docs/demo';
const info = JSON.parse(readFileSync('.demo/demo.local.json', 'utf8'));
const manager = info.logins.find((l) => l.role === 'workspace_admin');
const family = info.logins.find((l) => l.key === 'family') ?? info.logins.find((l) => l.role === 'member');
const personA = info.people[0];

mkdirSync(join(OUT, 'raw'), { recursive: true });

// A clean slate for the recording: resolve whatever the scripted scenarios left open, so the
// watch's own heart-rate question and fall are what the viewer sees.
{
  const form = new URLSearchParams({ email: manager.email, password: manager.password });
  const r = await fetch(`${API}/auth/cloud/login`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form.toString() });
  const tokens = await r.json();
  const headers = { Authorization: `Bearer ${tokens.access_token}`, 'X-Workspace-ID': info.workspace_id, 'Content-Type': 'application/json' };
  const all = await (await fetch(`${API}/api/v1/care/alerts?open_only=false&limit=200`, { headers })).json();
  const unresolved = (all.alerts ?? []).filter((a) => ['asking', 'open', 'escalating', 'acknowledged'].includes(a.status));
  for (const a of unresolved) {
    await fetch(`${API}/api/v1/care/alerts/${a.id}/resolve`, { method: 'POST', headers, body: JSON.stringify({ resolution: 'Resolved before the recording.' }) });
  }
  console.log('resolved before recording:', unresolved.length);
}
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, recordVideo: { dir: join(OUT, 'raw'), size: { width: 1280, height: 800 } } });
const page = await context.newPage();
const shot = async (name) => { await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: false }); console.log('screenshot', name); };
const pause = (ms) => page.waitForTimeout(ms);

// 1. Dashboard: sign in as the demo manager, people overview, a person's record.
await page.goto(`${DASH}/login`);
await page.waitForLoadState('networkidle');
// Typed before the page has hydrated, values sit in the DOM but not in React's state and the
// button stays disabled; so type, check, and type again until the page is live.
await page.getByText('Advanced').click();
for (let attempt = 0; attempt < 12; attempt++) {
  await page.getByLabel('Care server').fill(API);
  await page.getByLabel('Username').fill(manager.email);
  await page.getByLabel('Password').fill(manager.password);
  if (!(await page.getByRole('button', { name: /sign in/i }).isDisabled())) break;
  await pause(500);
}
await page.getByRole('button', { name: /sign in/i }).click();
await page.waitForURL((u) => !u.pathname.includes('login'), { timeout: 20000 });
await pause(1200); await shot('01-dashboard-people');
await page.goto(`${DASH}/people/${personA.id}?tab=home`); await pause(1500); await shot('02-dashboard-home-record');
await page.goto(`${DASH}/people/${personA.id}?tab=timeline`); await pause(1500); await shot('03-dashboard-timeline');

// 2. The watch in the browser (/watch), driven from the presenter tab: the device key goes in
//    there, a heart rate is sent, the watch asks, the person answers I'm OK, talks, falls, asks for help.
await page.goto(`${DASH}/watch`);
await page.waitForLoadState('networkidle');
const presenter = await context.newPage();
await presenter.goto(`${DASH}/presenter`);
await presenter.waitForLoadState('networkidle');
for (let attempt = 0; attempt < 12; attempt++) {
  await presenter.getByTestId('token').fill(personA.device_token);
  await pause(800);
  if ((await page.getByTestId('watch-face').textContent())?.includes('All well')) break;
}
await page.bringToFront();
await pause(2500); await shot('04-watch-paired');
await presenter.getByTestId('inject-heart').click();
await pause(4000); await shot('05-watch-are-you-ok');
await page.getByTestId('ok').click(); await pause(1500);
await page.getByTestId('typed').fill("what's on today?"); await page.getByTestId('typed').press('Enter'); await pause(3000); await shot('06-watch-talk');
await presenter.getByTestId('inject-fall').click(); await pause(4000); await shot('07-watch-fall-countdown');
await page.getByTestId('help').click(); await pause(2500); await shot('08-watch-help-sent');
const presenterVideo = await presenter.video()?.path();
await presenter.close();

// 3. Dashboard alerts: the open SOS with whereabouts.
await page.goto(`${DASH}/alerts`); await pause(2000); await shot('09-dashboard-alerts');

// 4. The family app on the web: sign in as the family member, alerts, I'm on my way.
await page.goto(FAMILY); await page.waitForLoadState('networkidle'); await pause(2500);
await page.getByTestId('advanced').click();
for (let attempt = 0; attempt < 12; attempt++) {
  await page.getByTestId('server').fill(API);
  await page.getByTestId('username').fill(family.email);
  await page.getByTestId('password').fill(family.password);
  const disabled = await page.getByTestId('sign-in').getAttribute('aria-disabled');
  if (disabled !== 'true') break;
  await pause(500);
}
await page.getByTestId('sign-in').click();
await pause(3500); await shot('10-family-home');
await page.getByText(/alerts/i).first().click().catch(() => {});
await pause(2500); await shot('11-family-alerts');
const ack = page.getByTestId(/^ack-/).first();
if (await ack.count()) { await ack.click(); await pause(2500); await shot('12-family-on-my-way'); }

await context.close();
await browser.close();
const presenterFile = presenterVideo ? presenterVideo.split('/').pop() : '';
const vids = readdirSync(join(OUT, 'raw')).filter((f) => f.endsWith('.webm') && f !== presenterFile);
if (vids.length) { renameSync(join(OUT, 'raw', vids[0]), join(OUT, 'raw', 'demo.webm')); console.log('video', join(OUT, 'raw', 'demo.webm')); }
