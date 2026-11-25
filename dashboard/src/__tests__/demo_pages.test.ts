import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { NextRequest } from 'next/server';
import { vi } from 'vitest';
import { demoPagesEnabled, isDemoPath } from '@/lib/demoPages';

const ROOT = join(__dirname, '..', '..', '..');

async function statusFor(path: string, flag: string | undefined) {
  vi.resetModules();
  vi.stubEnv('NEXT_PUBLIC_IRA_DEMO_PAGES', flag ?? '');
  const { middleware } = await import('@/middleware');
  const res = middleware(new NextRequest(`http://dash.invalid${path}`));
  vi.unstubAllEnvs();
  return res.status;
}

test('the demonstration pages answer 404 in any build not made for a demonstration', async () => {
  for (const path of ['/watch', '/presenter', '/simulator', '/watch/x']) {
    expect(await statusFor(path, undefined)).toBe(404);
    expect(await statusFor(path, '0')).toBe(404);
    expect(await statusFor(path, 'true')).toBe(404); // only the exact value turns them on
  }
});

test('a demonstration build serves them, and nothing else is affected', async () => {
  expect(await statusFor('/watch', '1')).toBe(200);
  expect(await statusFor('/presenter', '1')).toBe(200);
  expect(isDemoPath('/people/123')).toBe(false);
  expect(isDemoPath('/watchlist')).toBe(false);
  expect(demoPagesEnabled(undefined)).toBe(false);
});

test('the production image never turns them on, and the demo scripts do', () => {
  expect(readFileSync(join(ROOT, 'deploy', 'Dockerfile.dashboard'), 'utf8')).not.toMatch(/IRA_DEMO_PAGES/);
  expect(readFileSync(join(ROOT, 'scripts', 'demo', 'apps.sh'), 'utf8')).toMatch(/NEXT_PUBLIC_IRA_DEMO_PAGES=1/);
});
