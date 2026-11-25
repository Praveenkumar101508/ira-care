import nextConfig from '../../next.config';
import { NextRequest } from 'next/server';
import { middleware } from '@/middleware';

test('every page is sent with headers that stop framing, sniffing and referrer leaks', async () => {
  const rules = await nextConfig.headers!();
  const all = rules.find((r) => r.source === '/:path*');
  expect(all).toBeDefined();
  const h = Object.fromEntries(all!.headers.map((x) => [x.key.toLowerCase(), x.value]));
  // The policy is per request (it carries a nonce), so it comes from the middleware, once.
  expect(h['content-security-policy']).toBeUndefined();
  const csp = middleware(new NextRequest('http://dash.invalid/')).headers.get('content-security-policy')!;
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain("object-src 'none'");
  expect(h['x-frame-options']).toBe('DENY');
  expect(h['x-content-type-options']).toBe('nosniff');
  expect(h['referrer-policy']).toBe('no-referrer');
  expect(h['permissions-policy']).toContain('camera=()');
  expect(h['permissions-policy']).toContain('microphone=(self)');
  expect(nextConfig.poweredByHeader).toBe(false);
});
