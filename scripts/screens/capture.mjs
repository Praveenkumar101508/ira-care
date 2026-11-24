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
const family = info.logins.find((l) => l.role === 'member');
const person = info.people[0];
mkdirSync(OUT, { recursive: true });

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
  await page.bringToFront(); await pause(page, 1500); await shot(page, 'watch-all-well', size);
  await presenter.getByTestId('inject-heart').click(); await pause(page, 4000); await shot(page, 'watch-question', size);
  await page.getByTestId('ok').click(); await pause(page, 1500);
  await presenter.getByTestId('inject-fall').click(); await pause(page, 3000); await shot(page, 'watch-fall', size);
  await page.getByTestId('ok').click(); await pause(page, 1000);
  await presenter.bringToFront(); await pause(presenter, 500); await shot(presenter, 'presenter', size);
  await presenter.close();

  // The phone app on the web: sign in as the care manager (carer experience), then the family member.
  for (const [who, login] of [['carer', manager], ['family', family]]) {
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
writeFileSync(join(OUT, 'INDEX.md'), `# Every screen, phone and desktop\n\nTaken by \`scripts/screens/capture.mjs\` against the demo stack. The wearer's own phone experience needs a circle member whose relationship is "self"; the demo seed has none yet (phase 13 adds one).\n\n| Screen | Phone (390 × 844) | Desktop (1280 × 800) |\n|---|---|---|\n${rows.join('\n')}\n`);
console.log('screens:', taken.length);
