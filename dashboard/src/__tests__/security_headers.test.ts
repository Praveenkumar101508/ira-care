import nextConfig from '../../next.config';

test('every page is sent with headers that stop framing, sniffing and referrer leaks', async () => {
  const rules = await nextConfig.headers!();
  const all = rules.find((r) => r.source === '/:path*');
  expect(all).toBeDefined();
  const h = Object.fromEntries(all!.headers.map((x) => [x.key.toLowerCase(), x.value]));
  expect(h['content-security-policy']).toContain("frame-ancestors 'none'");
  expect(h['content-security-policy']).toContain("object-src 'none'");
  expect(h['x-frame-options']).toBe('DENY');
  expect(h['x-content-type-options']).toBe('nosniff');
  expect(h['referrer-policy']).toBe('no-referrer');
  expect(h['permissions-policy']).toContain('camera=()');
  expect(h['permissions-policy']).toContain('microphone=(self)');
  expect(nextConfig.poweredByHeader).toBe(false);
});
