import { ApiError, CareClient } from '../api/client';

type Handler = (url: string, init?: RequestInit) => Promise<{ status: number; body?: unknown }> | { status: number; body?: unknown };
function fakeFetch(handler: Handler) {
  const calls: { url: string; auth: string }[] = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, auth: String((init?.headers as Record<string, string>)?.Authorization ?? '') });
    const out = await handler(url, init);
    return { ok: out.status >= 200 && out.status < 300, status: out.status, json: async () => out.body, text: async () => '' } as unknown as Response;
  }) as unknown as typeof fetch;
  return { f, calls };
}
const session = { baseUrl: 'http://care.invalid', accessToken: 'old', refreshToken: 'refresh-1', workspaceId: 'ws-1' };

test('an expired sign-in is renewed once, for every call that met it at the same time, and each call is retried once', async () => {
  let renewals = 0;
  const { f, calls } = fakeFetch(async (url, init) => {
    if (url.endsWith('/auth/refresh')) { renewals += 1; await new Promise((r) => setTimeout(r, 10)); return { status: 200, body: { access_token: 'new' } }; }
    const auth = (init?.headers as Record<string, string>).Authorization;
    return auth === 'Bearer new' ? { status: 200, body: [] } : { status: 401 };
  });
  const c = new CareClient(session, f);
  const kept: string[] = [];
  c.onRenewed = (t) => kept.push(t);
  await Promise.all([c.people(), c.alerts(false), c.people()]);
  expect(renewals).toBe(1);
  expect(kept).toEqual(['new']);
  expect(calls.filter((x) => x.url.endsWith('/auth/refresh'))[0].auth).toBe('Bearer refresh-1');
  expect(calls.filter((x) => !x.url.endsWith('/auth/refresh')).length).toBe(6); // three, each sent twice
});

test('a refused renewal is final: one try, no loop, and a plain message to sign in again', async () => {
  let renewals = 0;
  const { f } = fakeFetch((url) => { if (url.endsWith('/auth/refresh')) { renewals += 1; return { status: 401 }; } return { status: 401 }; });
  const c = new CareClient(session, f);
  await expect(c.people()).rejects.toMatchObject({ status: 401, message: expect.stringContaining('sign in again') });
  expect(renewals).toBe(1);
});

test('still refused after a renewal: final, never a second renewal for the same call', async () => {
  let renewals = 0;
  const { f } = fakeFetch((url) => { if (url.endsWith('/auth/refresh')) { renewals += 1; return { status: 200, body: { access_token: 'new' } }; } return { status: 401 }; });
  await expect(new CareClient(session, f).people()).rejects.toBeInstanceOf(ApiError);
  expect(renewals).toBe(1);
});

test('no network while renewing is not an expired sign-in: nobody is signed out for it', async () => {
  const { f } = fakeFetch((url) => { if (url.endsWith('/auth/refresh')) throw new TypeError('Network request failed'); return { status: 401 }; });
  await expect(new CareClient(session, f).people()).rejects.toMatchObject({ status: 0 });
  const { f: f2 } = fakeFetch((url) => (url.endsWith('/auth/refresh') ? { status: 503 } : { status: 401 }));
  await expect(new CareClient(session, f2).people()).rejects.toMatchObject({ status: 0 });
});

test('without a refresh token a 401 is final at once', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 401 }));
  await expect(new CareClient({ ...session, refreshToken: undefined }, f).people()).rejects.toMatchObject({ status: 401 });
  expect(calls.some((x) => x.url.endsWith('/auth/refresh'))).toBe(false);
});
