import { ApiError, CareClient, parseTalk } from '../api/client';

function fakeFetch(handler: (url: string, init?: RequestInit) => { status: number; body?: unknown }) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const out = handler(url, init);
    return {
      ok: out.status >= 200 && out.status < 300,
      status: out.status,
      json: async () => out.body,
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return { f, calls };
}

const session = { baseUrl: 'http://care.invalid', accessToken: 'tok', workspaceId: 'ws-1' };

test('login signs in by email on a cloud deployment and returns the tokens', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200, body: { access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 900 } }));
  const out = await CareClient.login('http://care.invalid', 'fam@example.invalid', 'pw', f);
  expect(out.access_token).toBe('a');
  expect(calls[0].url).toBe('http://care.invalid/auth/cloud/login');
  expect(String(calls[0].init?.body)).toContain('email=fam%40example.invalid');
  expect((calls[0].init?.headers as Record<string, string>)['Content-Type']).toContain('x-www-form-urlencoded');
});

test('a private deployment has no cloud login (404) and the owner account route is used instead', async () => {
  const { f, calls } = fakeFetch((url) => url.endsWith('/auth/cloud/login') ? { status: 404 } : { status: 200, body: { access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 900 } });
  const out = await CareClient.login('http://care.invalid', 'owner', 'pw', f);
  expect(out.access_token).toBe('a');
  expect(calls.map((c) => c.url)).toEqual(['http://care.invalid/auth/cloud/login', 'http://care.invalid/auth/token']);
  expect(String(calls[1].init?.body)).toContain('username=owner');
});

test('a wrong password is a plain message, not a stack trace', async () => {
  const { f } = fakeFetch(() => ({ status: 401, body: { detail: 'nope' } }));
  await expect(CareClient.login('http://care.invalid', 'fam', 'bad', f)).rejects.toThrow('Wrong username or password.');
});

test('care calls carry the bearer token and the workspace header', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200, body: { people: [{ id: 'p1', display_name: 'Test Person' }] } }));
  const people = await new CareClient(session, f).people();
  expect(people[0].id).toBe('p1');
  const headers = calls[0].init?.headers as Record<string, string>;
  expect(headers.Authorization).toBe('Bearer tok');
  expect(headers['X-Workspace-ID']).toBe('ws-1');
});

test('without a selected workspace a care call refuses before the network', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200 }));
  await expect(new CareClient({ ...session, workspaceId: null }, f).alerts()).rejects.toBeInstanceOf(ApiError);
  expect(calls).toHaveLength(0);
});

test('acknowledging sends on_my_way and 401 asks to sign in again', async () => {
  const { f, calls } = fakeFetch((url) => url.endsWith('/acknowledge') ? { status: 200, body: { id: 'a1', status: 'acknowledged' } } : { status: 401 });
  const c = new CareClient(session, f);
  const acked = await c.acknowledge('a1', 'five minutes');
  expect(acked.status).toBe('acknowledged');
  expect(JSON.parse(String(calls[0].init?.body))).toEqual({ kind: 'on_my_way', note: 'five minutes' });
  await expect(c.alerts()).rejects.toThrow('Please sign in again.');
});

test('signing out tells the server with the refresh token first, then the access token', async () => {
  const { f, calls } = fakeFetch(() => ({ status: 200, body: { message: 'Logged out' } }));
  const ok = await new CareClient({ ...session, refreshToken: 'ref' }, f).logout();
  expect(ok).toBe(true);
  expect(calls.map((c) => c.url)).toEqual(['http://care.invalid/auth/logout', 'http://care.invalid/auth/logout']);
  expect(calls.map((c) => (c.init?.headers as Record<string, string>).Authorization)).toEqual(['Bearer ref', 'Bearer tok']);
  expect(calls.every((c) => c.init?.method === 'POST')).toBe(true);
});

test('signing out never throws: an unreachable server is reported as not confirmed', async () => {
  const f = (async () => { throw new TypeError('Network request failed'); }) as unknown as typeof fetch;
  await expect(new CareClient({ ...session, refreshToken: 'ref' }, f).logout()).resolves.toBe(false);
  const { f: refused } = fakeFetch(() => ({ status: 503, body: { detail: 'later' } }));
  await expect(new CareClient(session, refused).logout()).resolves.toBe(false);
});

test('medication and the care plan are read for the person record', async () => {
  const { f, calls } = fakeFetch((url) => url.endsWith('/medications')
    ? { status: 200, body: { medications: [{ id: 'm1', name: 'the morning tablets', dose: 'one', times: ['08:00:00'], notes: '', active: true }], count: 1 } }
    : { status: 200, body: { plan: null } });
  const c = new CareClient(session, f);
  expect((await c.medications('p1'))[0].name).toBe('the morning tablets');
  expect(await c.plan('p1')).toBeNull();
  expect(calls.map((x) => x.url)).toEqual(['http://care.invalid/api/v1/care/people/p1/medications', 'http://care.invalid/api/v1/care/people/p1/plan']);
});

test('a dead network becomes an offline message', async () => {
  const f = (async () => { throw new TypeError('Network request failed'); }) as unknown as typeof fetch;
  await expect(new CareClient(session, f).people()).rejects.toThrow('No connection');
});

test('server detail text is passed through for other errors', async () => {
  const { f } = fakeFetch(() => ({ status: 404, body: { detail: 'Person not found.' } }));
  await expect(new CareClient(session, f).status('x')).rejects.toThrow('Person not found.');
});

test('a streamed talk reply is read line by line: sentences, then the action', () => {
  const raw = '{"sentence": "Here is Family Member."}\n{"sentence": "Tap to call."}\n{"done": true, "action": {"kind": "call", "name": "Family Member", "phone": "+440000000001"}, "source": "rules", "needs_help": false}\n';
  const r = parseTalk(raw);
  expect(r.sentences).toEqual(['Here is Family Member.', 'Tap to call.']);
  expect(r.action).toEqual({ kind: 'call', name: 'Family Member', phone: '+440000000001' });
  expect(r.needsHelp).toBe(false);
  expect(parseTalk('garbage\n{"done": true, "action": {"kind": "safety_check"}, "needs_help": true}').needsHelp).toBe(true);
});
