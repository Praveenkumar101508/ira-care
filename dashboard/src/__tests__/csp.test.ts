import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { NextRequest } from 'next/server';
import { vi } from 'vitest';
import { apiOrigin, buildContentSecurityPolicy, makeNonce } from '@/lib/securityHeaders';

/** Directive name -> its sources. */
function parse(csp: string): Record<string, string[]> {
  return Object.fromEntries(
    csp.split(';').map((d) => d.trim()).filter(Boolean).map((d) => {
      const [name, ...values] = d.split(/\s+/);
      return [name.toLowerCase(), values];
    }),
  );
}

/**
 * What a strict production policy must hold, as a list of problems (empty is good). The tests
 * run it on the real policy and, as negative controls, on weakened ones it must reject.
 */
function problems(csp: string, nonce: string): string[] {
  const p = parse(csp);
  const out: string[] = [];
  const scripts = p['script-src'] ?? p['default-src'] ?? [];
  if (!scripts.includes(`'nonce-${nonce}'`)) out.push('script-src lacks the nonce');
  if (!scripts.includes("'strict-dynamic'")) out.push("script-src lacks 'strict-dynamic'");
  for (const bad of ["'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", '*', 'data:', 'blob:']) {
    if (scripts.includes(bad)) out.push(`script-src allows ${bad}`);
  }
  for (const d of ['script-src-elem', 'script-src-attr']) if (p[d]) out.push(`${d} overrides script-src`);
  if ((p['object-src'] ?? []).join(' ') !== "'none'") out.push("object-src is not 'none'");
  if (!["'none'", "'self'"].includes((p['base-uri'] ?? []).join(' '))) out.push('base-uri is not locked');
  if ((p['frame-ancestors'] ?? []).join(' ') !== "'none'") out.push("frame-ancestors is not 'none'");
  if ((p['form-action'] ?? []).join(' ') !== "'self'") out.push("form-action is not 'self'");
  for (const d of ['style-src', 'style-src-elem']) if ((p[d] ?? []).includes("'unsafe-inline'")) out.push(`${d} allows 'unsafe-inline'`);
  const connect = p['connect-src'] ?? [];
  if (connect.some((s) => s === '*' || s === 'https:' || s === 'http:' || s.includes('*'))) out.push('connect-src is open');
  return out;
}

const NONCE = 'bm9uY2Utbm9uY2Utbm9uY2U=';
const prod = (apiBaseUrl: string | undefined) => buildContentSecurityPolicy({ nonce: NONCE, dev: false, apiBaseUrl });

test('the production policy is strict: nonce and strict-dynamic for scripts, nothing unsafe', () => {
  for (const api of ['http://127.0.0.1:8010', 'https://care.example.org/', '', undefined]) {
    expect(problems(prod(api), NONCE)).toEqual([]);
  }
  expect(prod('https://care.example.org')).not.toContain('unsafe-eval');
  expect(prod('https://care.example.org')).not.toMatch(/script-src[^;]*unsafe-inline/);
});

test('negative controls: the check rejects policies that let injected script run', () => {
  const good = prod('https://care.example.org');
  expect(problems(good.replace("'strict-dynamic'", "'strict-dynamic' 'unsafe-inline'"), NONCE)).toContain("script-src allows 'unsafe-inline'");
  expect(problems(good.replace("'strict-dynamic'", "'strict-dynamic' 'unsafe-eval'"), NONCE)).toContain("script-src allows 'unsafe-eval'");
  expect(problems(good.replace(`'nonce-${NONCE}' `, ''), NONCE)).toContain('script-src lacks the nonce');
  expect(problems(good.replace("object-src 'none'", "object-src 'self'"), NONCE)).toContain("object-src is not 'none'");
  expect(problems(good.replace("frame-ancestors 'none'", 'frame-ancestors *'), NONCE)).toContain("frame-ancestors is not 'none'");
  expect(problems(good.replace("connect-src 'self'", "connect-src 'self' https:"), NONCE)).toContain('connect-src is open');
  expect(problems(good + "; script-src-elem 'unsafe-inline'", NONCE)).toContain('script-src-elem overrides script-src');
  // The policy before this change (no script-src at all) fails too.
  const before = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'";
  expect(problems(before, NONCE)).toContain('script-src lacks the nonce');
});

test("'unsafe-eval' only under next dev", () => {
  const dev = parse(buildContentSecurityPolicy({ nonce: NONCE, dev: true, apiBaseUrl: 'http://localhost:8000' }));
  expect(dev['script-src']).toContain("'unsafe-eval'");
  expect(dev['script-src']).not.toContain("'unsafe-inline'");
  expect(dev['style-src-elem']).toEqual(["'self'", "'unsafe-inline'"]); // the dev error overlay; never in a build
  expect(parse(prod('http://localhost:8000'))['style-src-elem']).not.toContain("'unsafe-inline'");
  expect(parse(prod('http://localhost:8000'))['script-src']).not.toContain("'unsafe-eval'");
});

test('connect-src is this origin and the configured API origin, nothing else', () => {
  expect(parse(prod('https://care.example.org/api/'))['connect-src']).toEqual(["'self'", 'https://care.example.org']);
  expect(parse(prod('http://127.0.0.1:8010'))['connect-src']).toEqual(["'self'", 'http://127.0.0.1:8010']);
  expect(parse(prod(''))['connect-src']).toEqual(["'self'"]); // the production image: API behind the same origin
  expect(parse(prod('javascript:alert(1)'))['connect-src']).toEqual(["'self'"]);
  expect(apiOrigin('not a url')).toBeNull();
});

test('styles: sheets from this origin or with the nonce; only style attributes are inline', () => {
  const p = parse(prod('https://care.example.org'));
  expect(p['style-src']).toEqual(["'self'", `'nonce-${NONCE}'`]);
  expect(p['style-src-elem']).toEqual(["'self'", `'nonce-${NONCE}'`]);
  expect(p['style-src-attr']).toEqual(["'unsafe-inline'"]);
  expect(p['img-src']).toEqual(["'self'"]);
});

test('upgrade-insecure-requests only for a page served over HTTPS', () => {
  const p = (secure: boolean, dev = false) => parse(buildContentSecurityPolicy({ nonce: NONCE, dev, apiBaseUrl: '', secure }));
  expect(p(true)).toHaveProperty('upgrade-insecure-requests');
  expect(p(false)).not.toHaveProperty('upgrade-insecure-requests');
  expect(p(true, true)).not.toHaveProperty('upgrade-insecure-requests');
});

test('nonces are fresh 128-bit values, and a malformed one is refused', () => {
  const a = makeNonce();
  const b = makeNonce();
  expect(a).not.toBe(b);
  expect(atob(a)).toHaveLength(16);
  expect(() => buildContentSecurityPolicy({ nonce: "x' 'unsafe-inline", dev: false, apiBaseUrl: '' })).toThrow();
});

async function run(path: string, env: Record<string, string> = {}, headers: Record<string, string> = {}) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
  const { middleware, config } = await import('@/middleware');
  const res = middleware(new NextRequest(`http://dash.invalid${path}`, { headers }));
  vi.unstubAllEnvs();
  return { res, config };
}

test('the middleware sends a strict policy with a fresh nonce, and hands the nonce to Next.js', async () => {
  const { res } = await run('/people/abc', { NEXT_PUBLIC_CARE_API_BASE_URL: 'http://127.0.0.1:8010' });
  const csp = res.headers.get('content-security-policy')!;
  const nonce = res.headers.get('x-middleware-request-x-nonce')!;
  expect(nonce).toBeTruthy();
  expect(problems(csp, nonce)).toEqual([]);
  // Next.js reads the nonce from the request's policy; it must be the same policy.
  expect(res.headers.get('x-middleware-request-content-security-policy')).toBe(csp);
  expect(parse(csp)['connect-src']).toEqual(["'self'", 'http://127.0.0.1:8010']);
  const again = (await run('/people/abc')).res.headers.get('x-middleware-request-x-nonce');
  expect(again).not.toBe(nonce);
  // The production image: API behind the same origin, page HTTPS through the reverse proxy.
  const behindProxy = (await run('/', { NEXT_PUBLIC_CARE_API_BASE_URL: '' }, { 'x-forwarded-proto': 'https' })).res.headers.get('content-security-policy')!;
  expect(parse(behindProxy)['connect-src']).toEqual(["'self'"]);
  expect(parse(behindProxy)).toHaveProperty('upgrade-insecure-requests');
  expect(csp).not.toContain('upgrade-insecure-requests');
});

test('the middleware leaves eval off unless this is next dev', async () => {
  const test = (await run('/')).res.headers.get('content-security-policy')!;
  expect(test).not.toContain('unsafe-eval');
  const prodRes = (await run('/', { NODE_ENV: 'production' })).res.headers.get('content-security-policy')!;
  expect(prodRes).not.toContain('unsafe-eval');
  const dev = (await run('/', { NODE_ENV: 'development' })).res.headers.get('content-security-policy')!;
  expect(dev).toContain("'unsafe-eval'");
});

test('every page is covered by the middleware, and the demonstration pages still answer 404', async () => {
  const { res, config } = await run('/watch');
  expect(res.status).toBe(404);
  const matchers = config.matcher.map((m: string) => new RegExp(`^${m}$`));
  const covered = (p: string) => matchers.some((r: RegExp) => r.test(p));
  for (const p of ['/', '/alerts', '/people/123', '/people/123?tab=care', '/login', '/visits', '/team', '/search', '/watch', '/presenter']) {
    expect(covered(p.split('?')[0])).toBe(true);
  }
  expect(covered('/_next/static/chunks/main.js')).toBe(false);
  // Negative control: the matcher before this change covered only the demonstration pages.
  const before = ['/watch/:path*', '/presenter/:path*', '/simulator/:path*'].map((m) => new RegExp(`^${m.replace('/:path*', '(/.*)?')}$`));
  expect(before.some((r) => r.test('/people/123'))).toBe(false);
});

test('the root layout reads the request headers, so pages render per request with the nonce', () => {
  const layout = readFileSync(join(__dirname, '..', 'app', 'layout.tsx'), 'utf8');
  expect(layout).toMatch(/await headers\(\)/);
  for (const page of ['page.tsx', 'alerts/page.tsx', 'people/[id]/page.tsx']) {
    const src = readFileSync(join(__dirname, '..', 'app', page), 'utf8');
    expect(src).not.toMatch(/export const dynamic\s*=\s*['"]force-static/);
  }
});
