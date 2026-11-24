import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';

export const API = process.env.IRA_CARE_API ?? 'http://127.0.0.1:8010';
export const DASH = process.env.DASHBOARD_URL ?? 'http://localhost:3010';
export const FAMILY = process.env.FAMILY_URL ?? 'http://localhost:8091';
export const INFO_PATH = process.env.IRA_DEMO_INFO ?? '.demo/demo.local.json';

export type Login = { key?: string; name?: string; email: string; password: string; role: string };
export type Info = { workspace_id: string; logins: Login[]; people: { id: string; display_name: string; device_token: string }[] };

export function info(): Info { return JSON.parse(readFileSync(INFO_PATH, 'utf8')); }
export function login(i: Info, key: string): Login {
  return i.logins.find((l) => l.key === key) ?? i.logins.find((l) => (key === 'coordinator' ? l.role === 'workspace_admin' : l.role === 'member'))!;
}

/** The API as a signed-in person: the same calls the apps make. */
const apiCache = new Map<string, Api>();
export class Api {
  private constructor(private base: string, private headers: Record<string, string>) {}
  /** One sign-in per person for the whole run: the server rate-limits sign-ins, as it should. */
  static async signIn(i: Info, l: Login): Promise<Api> {
    const cached = apiCache.get(l.email);
    if (cached) return cached;
    const form = new URLSearchParams({ email: l.email, password: l.password });
    const r = await fetch(`${API}/auth/cloud/login`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form.toString() });
    if (!r.ok) throw new Error(`sign-in failed: ${r.status} ${await r.text()}`);
    const tokens = await r.json();
    const api = new Api(API, { Authorization: `Bearer ${tokens.access_token}`, 'X-Workspace-ID': i.workspace_id, 'Content-Type': 'application/json' });
    apiCache.set(l.email, api);
    return api;
  }
  async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const r = await fetch(`${this.base}${path}`, { method, headers: this.headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${await r.text()}`);
    return r.status === 204 ? (undefined as T) : (await r.json() as T);
  }
}

const dashboardSessions = new Map<string, string>();
/**
 * Sign in to the dashboard through its own form (the server field is behind "Advanced") the
 * first time; later pages for the same person reuse that session, since the server
 * rate-limits sign-ins and a real person signs in once.
 */
export async function dashboardSignIn(page: Page, l: Login) {
  const saved = dashboardSessions.get(l.email);
  if (saved) {
    await page.goto(`${DASH}/login`);
    await page.evaluate((v) => window.sessionStorage.setItem('ira_care_dashboard_session', v), saved);
    await page.goto(`${DASH}/`); await page.waitForLoadState('networkidle');
    return;
  }
  await page.goto(`${DASH}/login`); await page.waitForLoadState('networkidle');
  await page.getByText('Advanced').click();
  for (let attempt = 0; attempt < 12; attempt++) {
    await page.getByLabel('Care server').fill(API);
    await page.getByLabel('Username').fill(l.email);
    await page.getByLabel('Password').fill(l.password);
    if (!(await page.getByRole('button', { name: /sign in/i }).isDisabled())) break;
    await page.waitForTimeout(500);
  }
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL((u) => !u.pathname.includes('login'), { timeout: 30_000 });
  const session = await page.evaluate(() => window.sessionStorage.getItem('ira_care_dashboard_session'));
  if (session) dashboardSessions.set(l.email, session);
}

/** Sign in to the phone app on the web. */
export async function appSignIn(page: Page, l: Login) {
  await page.goto(FAMILY); await page.waitForLoadState('networkidle'); await page.waitForTimeout(1500);
  await page.getByTestId('advanced').click();
  for (let attempt = 0; attempt < 12; attempt++) {
    await page.getByTestId('server').fill(API);
    await page.getByTestId('username').fill(l.email);
    await page.getByTestId('password').fill(l.password);
    if ((await page.getByTestId('sign-in').getAttribute('aria-disabled')) !== 'true') break;
    await page.waitForTimeout(500);
  }
  await page.getByTestId('sign-in').click();
  await page.waitForTimeout(2500);
}

export const now = () => Date.now();
export const since = (t: number) => Math.round((Date.now() - t) / 100) / 10;
