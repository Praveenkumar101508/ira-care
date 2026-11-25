import { Api, ApiError } from '@/lib/api';

type Handler = (url: string, init?: RequestInit) => Promise<{ status: number; body?: unknown }> | { status: number; body?: unknown };

function fakeFetch(handler: Handler) {
  const calls: { url: string; auth: string }[] = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, auth: (init?.headers as Record<string, string>)?.Authorization ?? '' });
    const out = await handler(url, init);
    return { ok: out.status < 300, status: out.status, json: async () => out.body } as unknown as Response;
  }) as unknown as typeof fetch;
  return { f, calls };
}

const base = { baseUrl: 'http://care.invalid', accessToken: 'old', refreshToken: 'refresh-1', workspaceId: 'ws', role: 'member' };
const isRefresh = (u: string) => u.endsWith('/auth/refresh');

test('an expired sign-in is renewed once and the request sent again with the new token', async () => {
  const { f, calls } = fakeFetch((url, init) => {
    if (isRefresh(url)) return { status: 200, body: { access_token: 'new' } };
    const auth = (init?.headers as Record<string, string>).Authorization;
    return auth === 'Bearer new' ? { status: 200, body: { people: [] } } : { status: 401 };
  });
  const api = new Api(base, f);
  const told: string[] = [];
  api.onRenewed = (t) => told.push(t);
  await expect(api.people()).resolves.toEqual([]);
  expect(calls.map((c) => c.auth)).toEqual(['Bearer old', 'Bearer refresh-1', 'Bearer new']);
  expect(told).toEqual(['new']);
  // The next call uses the new token straight away.
  await api.people();
  expect(calls.at(-1)?.auth).toBe('Bearer new');
  expect(calls.filter((c) => isRefresh(c.url))).toHaveLength(1);
});

test('the whole page renews once when several calls meet the expired token together', async () => {
  let renewals = 0;
  const { f, calls } = fakeFetch(async (url, init) => {
    if (isRefresh(url)) { renewals += 1; await new Promise((r) => setTimeout(r, 10)); return { status: 200, body: { access_token: 'new' } }; }
    return (init?.headers as Record<string, string>).Authorization === 'Bearer new' ? { status: 200, body: { people: [], alerts: [] } } : { status: 401 };
  });
  // Separate Api objects, as separate components on one page hold.
  await Promise.all([new Api(base, f).people(), new Api(base, f).alerts(true), new Api(base, f).people()]);
  expect(renewals).toBe(1);
  expect(calls.filter((c) => !isRefresh(c.url))).toHaveLength(6);
});

test('a refused refresh token ends the sign-in, without looping', async () => {
  let renewals = 0;
  const { f, calls } = fakeFetch((url) => { if (isRefresh(url)) { renewals += 1; return { status: 401 }; } return { status: 401 }; });
  await expect(new Api(base, f).people()).rejects.toMatchObject({ status: 401 });
  expect(renewals).toBe(1);
  expect(calls).toHaveLength(2);
});

test('a second 401 after a renewal is final', async () => {
  let renewals = 0;
  const { f } = fakeFetch((url) => { if (isRefresh(url)) { renewals += 1; return { status: 200, body: { access_token: 'new' } }; } return { status: 401 }; });
  await expect(new Api(base, f).people()).rejects.toMatchObject({ status: 401 });
  expect(renewals).toBe(1);
});

test('a server that cannot be reached during renewal does not sign the carer out', async () => {
  for (const refresh of [() => { throw new TypeError('network'); }, () => ({ status: 503 }), () => ({ status: 429 })]) {
    const { f } = fakeFetch((url) => (isRefresh(url) ? refresh() : { status: 401 }));
    const err = await new Api(base, f).people().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  }
});

test('without a refresh token a 401 is final and nothing is renewed', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 401 }));
  await expect(new Api({ ...base, refreshToken: undefined }, f).people()).rejects.toMatchObject({ status: 401 });
  expect(calls).toHaveLength(1);
});
