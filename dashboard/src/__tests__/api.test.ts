import { Api, ApiError } from '@/lib/api';

function fakeFetch(handler: (url: string, init?: RequestInit) => { status: number; body?: unknown }) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const out = handler(url, init);
    return { ok: out.status < 300, status: out.status, json: async () => out.body } as unknown as Response;
  }) as unknown as typeof fetch;
  return { f, calls };
}

const session = { baseUrl: 'http://care.invalid', accessToken: 'tok', workspaceId: 'ws', role: 'workspace_admin' };

test('every care call carries the bearer token and the workspace header', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200, body: { people: [] } }));
  await new Api(session, f).people();
  const h = calls[0].init?.headers as Record<string, string>;
  expect(h.Authorization).toBe('Bearer tok');
  expect(h['X-Workspace-ID']).toBe('ws');
  expect(calls[0].url).toBe('http://care.invalid/api/v1/care/people?include_archived=false');
});

test('login is a form post by email first, with the owner route only when the cloud route is absent', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 401 }));
  await expect(Api.login('http://care.invalid', 'carer@example.invalid', 'bad', f)).rejects.toThrow('Wrong username or password.');
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe('http://care.invalid/auth/cloud/login');
  expect(String(calls[0].init?.body)).toContain('email=carer%40example.invalid');
  const fallback = fakeFetch((url) => url.endsWith('/auth/cloud/login') ? { status: 404 } : { status: 200, body: { access_token: 'a', refresh_token: 'r' } });
  expect((await Api.login('http://care.invalid', 'owner', 'pw', fallback.f)).access_token).toBe('a');
  expect(String(fallback.calls[1].init?.body)).toContain('grant_type=password');
});

test('no workspace means no network call', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200 }));
  await expect(new Api({ ...session, workspaceId: null }, f).alerts(true)).rejects.toBeInstanceOf(ApiError);
  expect(calls).toHaveLength(0);
});

test('settings are put whole and errors carry the server detail', async () => {
  const { f, calls } = fakeFetch((url) => url.endsWith('/settings') ? { status: 422, body: { detail: 'heart_rate_max must be greater than heart_rate_min' } } : { status: 200 });
  const api = new Api(session, f);
  await expect(api.putSettings('p', { heart_rate_min: 120, heart_rate_max: 40 } as never)).rejects.toThrow('heart_rate_max must be greater');
  expect(calls[0].init?.method).toBe('PUT');
});
