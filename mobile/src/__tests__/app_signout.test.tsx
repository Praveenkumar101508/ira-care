import React from 'react';
import renderer, { act } from 'react-test-renderer';

// Account A answers slowly; account B answers at once.
type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void };
const defer = <T,>(): Deferred<T> => { let resolve!: (v: T) => void; const promise = new Promise<T>((r) => { resolve = r; }); return { promise, resolve }; };
const mockLateA = defer<unknown[]>();
const mockEvents: string[] = [];
let mockStored: Record<string, unknown> | null = { baseUrl: 'http://care.invalid', accessToken: 'A', refreshToken: 'rA', workspaceId: 'wsA', workspaceName: 'A', username: 'a' };

jest.mock('../storage', () => ({
  loadSession: async () => mockStored,
  saveSession: async (s: Record<string, unknown> | null) => { mockStored = s; mockEvents.push(s ? `save ${s.accessToken}` : 'session cleared'); },
  loadAppearance: async () => null,
  saveAppearance: async () => {},
  clearAppearance: async () => { mockEvents.push('appearance cleared'); },
}));
jest.mock('../push', () => ({
  registerForPush: async () => true,
  unregisterForPush: async () => { mockEvents.push('push unregistered'); },
}));
const mockPerson = (id: string, name: string) => ({ id, display_name: name, preferred_name: name, timezone: 'UTC', status: 'active' });
jest.mock('../api/client', () => {
  const actual = jest.requireActual('../api/client');
  class FakeClient {
    mockToken: string;
    constructor(mockSession: { accessToken: string }) { this.mockToken = mockSession.accessToken; }
    onRenewed?: (t: string) => void;
    people() { return this.mockToken === 'A' ? mockLateA.promise : Promise.resolve([mockPerson('pB', 'Bea')]); }
    alerts() { return Promise.resolve([]); }
    experience() { return Promise.resolve({ experience: 'family', memberships: [], can_manage: false }); }
    preferences() { return Promise.resolve({ appearance: {} }); }
    status(id: string) { return Promise.resolve({ person: mockPerson(id, id), devices: [], latest_readings: [], open_alerts: [], last_checkin: null }); }
    visits() { return Promise.resolve([]); } reminders() { return Promise.resolve([]); } circle() { return Promise.resolve([]); }
    consents() { return Promise.resolve({ consents: [] }); } putPreferences() { return Promise.resolve(); }
    workspaces() { return Promise.resolve({ workspaces: [{ id: 'wsB', name: 'B', role: 'member', slug: 'b', is_default: true, status: 'active' }] }); }
    static login() { return Promise.resolve({ access_token: 'B', refresh_token: 'rB', token_type: 'bearer', expires_in: 900 }); }
  }
  return { ...actual, CareClient: FakeClient };
});

import App from '../App';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null };
const all = (n: unknown, out: Node[] = []): Node[] => {
  if (Array.isArray(n)) { n.forEach((x) => all(x, out)); return out; }
  if (n && typeof n === 'object') { out.push(n as Node); ((n as Node).children ?? []).forEach((c) => all(c, out)); }
  return out;
};
const text = (tree: unknown) => all(tree).flatMap((x) => (x.children ?? []).filter((c) => typeof c === 'string') as string[]).join(' ');
const press = (r: renderer.ReactTestRenderer, id: string) => {
  const n = r.root.findAll((x) => x.props.testID === id && typeof x.props.onPress === 'function')[0];
  if (!n) throw new Error(`nothing to press: ${id}`);
  (n.props.onPress as () => void)();
};
const tick = () => act(async () => { await new Promise((res) => setTimeout(res, 20)); });

test('signing out unregisters push, forgets the look and the session on this phone', async () => {
  let r!: renderer.ReactTestRenderer;
  await act(async () => { r = renderer.create(<App />); });
  await tick();
  await act(async () => { press(r, 'tab-settings'); });
  await tick();
  await act(async () => { press(r, 'settings-account'); });
  await tick();
  await act(async () => { press(r, 'sign-out'); });
  await tick();
  expect(mockEvents).toEqual(expect.arrayContaining(['push unregistered', 'appearance cleared', 'session cleared']));
  await act(async () => { r.unmount(); });
});

test('a slow answer for the account that signed out never reaches the next account', async () => {
  mockStored = { baseUrl: 'http://care.invalid', accessToken: 'A', refreshToken: 'rA', workspaceId: 'wsA', workspaceName: 'A', username: 'a' };
  let r!: renderer.ReactTestRenderer;
  await act(async () => { r = renderer.create(<App />); });
  await tick();
  await act(async () => { press(r, 'tab-settings'); });
  await tick();
  await act(async () => { press(r, 'settings-account'); });
  await tick();
  await act(async () => { press(r, 'sign-out'); });
  await tick();
  // B signs in on the same running app, as a person would.
  const type = (id: string, v: string) => { const n = r.root.findAll((x) => x.props.testID === id && typeof x.props.onChangeText === 'function')[0]; (n.props.onChangeText as (t: string) => void)(v); };
  await act(async () => { type('username', 'b'); type('password', 'pw'); });
  await act(async () => { press(r, 'sign-in'); });
  await tick();
  expect(text(r.toJSON())).toContain('Bea');
  // ... and only then does A's late answer arrive.
  await act(async () => { mockLateA.resolve([mockPerson('pA', 'Ada-only-for-A')]); });
  await tick();
  expect(text(r.toJSON())).not.toContain('Ada-only-for-A');
  await act(async () => { r.unmount(); });
});
