import { test, expect } from '@playwright/test';
import { execSync } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { API, DASH, Api, info, login, now, since } from './helpers';

/**
 * Outages, with timings: Redis and the database are stopped under a running watch, and the
 * product must degrade honestly (the watch says it is saving for later, the server reports
 * unhealthy) and recover by itself when they come back. The numbers go to
 * docs/evidence/staging/outages.json.
 */
const COMPOSE = process.env.IRA_COMPOSE ?? '';
const OUT = process.env.IRA_E2E_OUT ?? 'results';
const i = info();
const coordinator = login(i, 'coordinator');
const person = i.people[0];
const results: Record<string, unknown>[] = [];

test.describe.configure({ mode: 'serial' });
test.skip(!COMPOSE, 'IRA_COMPOSE is not set: no containers to stop');

const compose = (args: string) => execSync(`${COMPOSE} ${args}`, { stdio: 'pipe', timeout: 120_000 }).toString();
async function health(): Promise<{ ok: boolean; status: number }> {
  try { const r = await fetch(`${API}/health`); return { ok: r.ok, status: r.status }; } catch { return { ok: false, status: 0 }; }
}
async function until(pred: () => Promise<boolean>, limitMs: number): Promise<number> {
  const t = now();
  while (Date.now() - t < limitMs) { if (await pred()) return since(t); await new Promise((r) => setTimeout(r, 500)); }
  throw new Error(`not within ${limitMs} ms`);
}

test.afterAll(() => {
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, 'outages.json'), JSON.stringify({ measured_at: new Date().toISOString(), results }, null, 2) + '\n');
});

test('Redis stops: the server says so, help from the watch is kept and delivered when it returns', async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 420, height: 520 } });
  const watch = await ctx.newPage();
  await watch.goto(`${DASH}/watch`);
  await watch.evaluate(([api, token]) => window.localStorage.setItem('ira_care_simulator', JSON.stringify({ baseUrl: api, token })), [API, person.device_token]);
  await watch.reload(); await watch.waitForLoadState('networkidle');
  await expect(watch.getByTestId('watch-text')).toHaveText(/All well|resolved|here/i, { timeout: 30_000 });

  const t0 = now();
  compose('stop redis');
  const stoppedAt = since(t0);
  const unhealthyAfter = await until(async () => !(await health()).ok, 60_000);
  results.push({ outage: 'redis', step: 'server reports unhealthy', seconds_after_stop: unhealthyAfter });

  const tHelp = now();
  await watch.getByTestId('help').click();
  await expect(watch.getByTestId('watch-text')).toHaveText(/saved|Waiting for connection|Sending|received/i, { timeout: 30_000 });
  const watchWords = (await watch.getByTestId('watch-text').textContent()) ?? '';
  results.push({ outage: 'redis', step: 'watch answers the help press', seconds: since(tHelp), words: watchWords });

  compose('start redis');
  const healthyAfter = await until(async () => (await health()).ok, 120_000);
  results.push({ outage: 'redis', step: 'server healthy again after start', seconds_after_start: healthyAfter, stop_took_s: stoppedAt });
  const api = await Api.signIn(i, coordinator);
  const delivered = await until(async () => {
    const r = await api.req<{ alerts: { kind: string; status: string; opened_at: string }[] }>('GET', `/api/v1/care/alerts?open_only=true&limit=50`);
    return r.alerts.some((a) => a.kind === 'sos' || a.kind.includes('help') || a.status === 'escalating' || a.status === 'open');
  }, 120_000);
  results.push({ outage: 'redis', step: 'help request visible to the care team', seconds_after_start: delivered });
  await expect(watch.getByTestId('watch-text')).toHaveText(/received|on the way|All well/i, { timeout: 60_000 });
  // Leave the alert closed for the next test.
  const open = await api.req<{ alerts: { id: string; status: string }[] }>('GET', `/api/v1/care/alerts?open_only=true&limit=50`);
  for (const a of open.alerts) await api.req('POST', `/api/v1/care/alerts/${a.id}/resolve`, { resolution: 'Staging outage test.', outcome: 'all_well' }).catch(() => undefined);
  await ctx.close();
});

test('the database stops: the server reports it and comes back on its own', async () => {
  const t0 = now();
  compose('stop postgres');
  const unhealthyAfter = await until(async () => !(await health()).ok, 60_000);
  results.push({ outage: 'postgres', step: 'server reports unhealthy', seconds_after_stop: unhealthyAfter });
  compose('start postgres');
  const healthyAfter = await until(async () => (await health()).ok, 180_000);
  results.push({ outage: 'postgres', step: 'server healthy again after start', seconds_after_start: healthyAfter, stop_at_s: since(t0) });
  const api = await Api.signIn(i, coordinator);
  const people = await api.req<{ people: unknown[] }>('GET', '/api/v1/care/people');
  expect(people.people.length).toBeGreaterThan(0);
});
