// Screenshot every screen of IRA CARE at phone and desktop sizes (phase 12).
//
//   node scripts/screens/capture.mjs
//
// Expects the demo API (IRA_CARE_API, default http://127.0.0.1:8000), the
// dashboard (DASHBOARD_URL, default http://localhost:3000), the phone app's web
// build (FAMILY_URL, default http://localhost:8081) and .demo/demo.local.json
// from scripts/demo/seed_demo.py. Writes docs/screens/<screen>.<phone|desktop>.png
// and docs/screens/INDEX.md. The presenter page is captured too, under its own
// name, so a reviewer can see it; it is never part of a recording.
//
// Each size starts clean. Taking the phone screens leaves alerts open on the
// demo server (the heart-rate readings and the fall), and the watch shows any
// open alert as "Help request received." over everything else, so before each
// size the care manager resolves every open alert through the API, and the
// script waits until the watch says "All well" before that screen (a
// just-resolved alert shows "This is now resolved." for a minute first).
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)('playwright');
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8000';
const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3000';
const FAMILY = process.env.FAMILY_URL ?? 'http://localhost:8081';
const OUT = process.env.SCREENS_OUT ?? 'docs/screens';
const info = JSON.parse(readFileSync('.demo/demo.local.json', 'utf8'));
const manager = info.logins.find((l) => l.role === 'workspace_admin');
const family = info.logins.find((l) => l.key === 'family') ?? info.logins.find((l) => l.role === 'member');
const wearer = info.logins.find((l) => l.key === 'wearer');
const person = info.people[0];
mkdirSync(OUT, { recursive: true });

/** Resolve every open alert in the demo organisation, as the care manager would. */
let managerToken = null; // signed in once: the sign-in route allows five a minute, and the screens use four of them
async function closeOpenAlerts() {
  const headers = async (fresh = false) => {
    if (!managerToken || fresh) {
      const login = await fetch(`${API}/auth/cloud/login`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ email: manager.email, password: manager.password }).toString() });
      if (!login.ok) throw new Error(`The care manager could not sign in to close open alerts (${login.status}).`);
      managerToken = (await login.json()).access_token;
    }
    return { Authorization: `Bearer ${managerToken}`, 'X-Workspace-ID': info.workspace_id, 'Content-Type': 'application/json' };
  };
  let h = await headers();
  if ((await fetch(`${API}/api/v1/care/ops/status`, { headers: h })).status === 401) h = await headers(true);
  const list = await fetch(`${API}/api/v1/care/alerts?open_only=true&limit=500`, { headers: h });
  if (!list.ok) throw new Error(`Could not list open alerts (${list.status}).`);
  const { alerts } = await list.json();
  for (const a of alerts) {
    const r = await fetch(`${API}/api/v1/care/alerts/${a.id}/resolve`, { method: 'POST', headers: h, body: JSON.stringify({ resolution: 'Closed before taking the screens.', outcome: 'other' }) });
    if (!r.ok && r.status !== 404) throw new Error(`Could not resolve alert ${a.id} (${r.status}).`);
  }
  // A stack started a moment ago has not run its care jobs yet, and every dashboard page would say
  // "Monitoring paused". Wait (up to three minutes) for the first pass of the jobs.
  for (let waited = 0; ; waited += 5000) {
    const ops = await fetch(`${API}/api/v1/care/ops/status`, { headers: h });
    if (ops.ok && !(await ops.json()).monitoring_paused) break;
    if (waited >= 180_000) throw new Error('The care jobs have not run; every dashboard page would say "Monitoring paused".');
    await new Promise((r) => setTimeout(r, 5000));
  }
  console.log(`closed ${alerts.length} open alert(s)${alerts.length ? ': ' + alerts.map((a) => `${a.kind} (${a.status})`).join(', ') : ''}`);
}

const SIZES = { phone: { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true }, desktop: { width: 1280, height: 800, deviceScaleFactor: 1 } };
const taken = [];
const browser = await chromium.launch();

async function shot(page, name, size) {
  const file = `${name}.${size}.png`;
  await page.screenshot({ path: join(OUT, file), fullPage: true });
  taken.push({ name, size, file });
  console.log('shot', file);
}
const pause = (page, ms) => page.waitForTimeout(ms);

async function dashboardSignIn(page, login) {
  await page.goto(`${DASH}/login`); await page.waitForLoadState('networkidle');
  await page.getByText('Advanced').click();
  for (let attempt = 0; attempt < 12; attempt++) {
    await page.getByLabel('Care server').fill(API);
    await page.getByLabel('Username').fill(login.email);
    await page.getByLabel('Password').fill(login.password);
    if (!(await page.getByRole('button', { name: /sign in/i }).isDisabled())) break;
    await pause(page, 500);
  }
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL((u) => !u.pathname.includes('login'), { timeout: 20000 });
}

for (const [size, viewport] of Object.entries(SIZES)) {
  await closeOpenAlerts();
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.deviceScaleFactor, isMobile: viewport.isMobile, hasTouch: viewport.hasTouch });
  const page = await context.newPage();

  // Dashboard, signed out
  for (const route of ['login', 'signup', 'join']) { await page.goto(`${DASH}/${route}`); await page.waitForLoadState('networkidle'); await shot(page, `dashboard-${route}`, size); }
  // Dashboard, signed in as the care manager
  await dashboardSignIn(page, manager);
  await pause(page, 1500); await shot(page, 'dashboard-overview', size);
  await page.goto(`${DASH}/alerts`); await pause(page, 1500); await shot(page, 'dashboard-alerts', size);
  await page.goto(`${DASH}/billing`); await pause(page, 1500); await shot(page, 'dashboard-billing', size);
  for (const tab of ['overview', 'profile', 'health', 'care', 'timeline', 'alerts', 'ira', 'reports', 'search', 'access']) {
    await page.goto(`${DASH}/people/${person.id}?tab=${tab}`); await pause(page, 1500); await shot(page, `dashboard-person-${tab}`, size);
  }
  // The watch in the browser: before pairing, paired, asked a question, a fall; and the presenter page.
  await context.clearCookies();
  await page.goto(`${DASH}/watch`); await page.evaluate(() => window.localStorage.clear()); await page.reload(); await page.waitForLoadState('networkidle');
  await pause(page, 800); await shot(page, 'watch-pairing', size);
  const presenter = await context.newPage();
  await presenter.goto(`${DASH}/presenter`); await presenter.waitForLoadState('networkidle');
  for (let attempt = 0; attempt < 12; attempt++) {
    await presenter.getByTestId('token').fill(person.device_token);
    await pause(page, 800);
    if ((await page.getByTestId('watch-face').textContent())?.includes('All well')) break;
  }
  await page.bringToFront(); await pause(page, 1500);
  let face = '';
  for (let waited = 0; waited < 90_000; waited += 2000) {
    face = (await page.getByTestId('watch-text').textContent())?.trim() ?? '';
    if (face === 'All well') break;
    await pause(page, 2000);
  }
  if (face !== 'All well') throw new Error(`The watch says "${face}" before the all-well screen (${size}); the demo is not clean.`);
  await shot(page, 'watch-all-well', size);
  const notHelpWords = async (screen) => {
    const words = (await page.getByTestId('watch-text').textContent())?.trim() ?? '';
    if (/^(Help request|Sending|Waiting for connection)|is on the way\.$|has arrived\.$/.test(words)) throw new Error(`The watch shows "${words}" on the ${screen} screen (${size}).`);
  };
  await presenter.getByTestId('inject-heart').click(); await pause(page, 4000); await notHelpWords('question'); await shot(page, 'watch-question', size);
  await page.getByTestId('ok').click(); await pause(page, 1500);
  await presenter.getByTestId('inject-fall').click(); await pause(page, 3000); await notHelpWords('fall'); await shot(page, 'watch-fall', size);
  await page.getByTestId('ok').click(); await pause(page, 1000);
  await presenter.bringToFront(); await pause(presenter, 500); await shot(presenter, 'presenter', size);
  // Back to a normal heart rate, as the wrist would report next: with the last reading still out of
  // range the server opens a fresh heart-rate alert part way through the app screens below.
  await presenter.getByRole('spinbutton').first().fill('72');
  await presenter.getByTestId('inject-heart').click(); await pause(presenter, 3000);
  await presenter.close();

  // The watch screens raise a heart-rate alert and a fall; close what is still open so the app
  // screens below show one consistent state rather than an alert opening part way through.
  await page.close();
  await closeOpenAlerts();

  // The phone app on the web: sign in as the care manager (carer experience), then the family member.
  for (const [who, login] of [['carer', manager], ['family', family], ...(wearer ? [['wearer', wearer]] : [])]) {
    const ctx2 = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.deviceScaleFactor, isMobile: viewport.isMobile, hasTouch: viewport.hasTouch });
    const app = await ctx2.newPage();
    await app.goto(FAMILY); await app.waitForLoadState('networkidle'); await pause(app, 2500);
    if (who === 'carer') await shot(app, 'app-sign-in', size);
    await app.getByTestId('advanced').click();
    for (let attempt = 0; attempt < 12; attempt++) {
      await app.getByTestId('server').fill(API);
      await app.getByTestId('username').fill(login.email);
      await app.getByTestId('password').fill(login.password);
      if ((await app.getByTestId('sign-in').getAttribute('aria-disabled')) !== 'true') break;
      await pause(app, 500);
    }
    await app.getByTestId('sign-in').click(); await pause(app, 3500);
    await shot(app, `app-${who}-home`, size);
    for (const tab of ['Care', 'Alerts', 'Settings']) {
      const t = app.getByTestId(`tab-${tab.toLowerCase()}`);
      if (await t.count()) { await t.click(); await pause(app, 2000); await shot(app, `app-${who}-${tab.toLowerCase()}`, size); }
    }
    await ctx2.close();
  }
  await context.close();
}
await browser.close();
const rows = [...new Set(taken.map((t) => t.name))].map((n) => `| ${n} | ![phone](${n}.phone.png) | ![desktop](${n}.desktop.png) |`);
writeFileSync(join(OUT, 'INDEX.md'), `# Every screen, phone and desktop\n\nTaken by \`scripts/screens/capture.mjs\` against the demo stack. The wearer's own phone experience is Ada Hartley's account (a circle member whose relationship is "self").\n\n| Screen | Phone (390 × 844) | Desktop (1280 × 800) |\n|---|---|---|\n${rows.join('\n')}\n`);
console.log('screens:', taken.length);
