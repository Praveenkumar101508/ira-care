import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { axe } from 'jest-axe';
import { vi } from 'vitest';
import { Api } from '@/lib/api';
import {
  classifyRefusal, forgetAdmins, isUuid, issueProblems, refusalMessage, statusWords, stepUpWords, targetProblems, STATUS_WORDS, type AdminMe, type Instruction,
} from '@/lib/deviceAdmin';
import { saveSession } from '@/lib/session';

vi.mock('next/link', () => ({ default: ({ children, href, ...rest }: { children: unknown; href: string }) => <a href={href} {...rest}>{children as never}</a> }));
vi.mock('next/navigation', () => ({ usePathname: () => '/admin/devices' }));

import DeviceAdminPage from '@/app/admin/devices/page';
import { TopBar } from '@/components/TopBar';

/**
 * The deployment administration screen (D-085, D-089) against a stand-in server: the real
 * page, the real signed-in client and the real stored session; only `fetch` is replaced.
 */
const BASE = 'http://care.invalid';
const TENANT = '11111111-1111-4111-8111-111111111111';
const WS = '22222222-2222-4222-8222-222222222222';
const DEV = '33333333-3333-4333-8333-333333333333';
const ME_ADMIN: AdminMe = { deployment_admin: true, auth_age_seconds: 20, step_up_required: false, max_age_seconds: 300, mfa_enrolled: false };
const ME_NOT: AdminMe = { deployment_admin: false, auth_age_seconds: null, step_up_required: false, max_age_seconds: 300, mfa_enrolled: false };

type Answer = { status: number; body?: unknown };
type Call = { method: string; path: string; auth: string; body: unknown };

/** A stand-in server: each call is answered by `route(method, path, body)` and recorded. */
function server(route: (method: string, path: string, body: unknown, auth: string) => Answer | undefined) {
  const calls: Call[] = [];
  const f = (async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const path = url.replace(BASE, '');
    const auth = (init?.headers as Record<string, string>)?.Authorization ?? '';
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method, path, auth, body });
    const out = route(method, path, body, auth) ?? { status: 404, body: { detail: 'Not found.' } };
    return { ok: out.status < 300, status: out.status, json: async () => out.body } as unknown as Response;
  }) as unknown as typeof fetch;
  vi.stubGlobal('fetch', f);
  return calls;
}

const row = (over: Partial<Instruction> = {}): Instruction => ({
  id: 'i1', device_id: DEV, action: 'MAINTENANCE_WINDOW', state: 'signed', status: 'signed', issued_at: '2026-11-26T10:00:00Z',
  expires_at: '2026-11-26T10:10:00Z', issued_by: 'admin-one', reason: 'Replace the strap sensor', delivered_at: null, revoked_at: null,
  revoked_by: null, outcome: null, outcome_reason: null, outcome_at: null, requested_at: null, approval_deadline: null, window_minutes: 10,
  approved_by: null, approved_at: null,
  events: [{ event: 'issued', actor_kind: 'deployment_admin', actor_id: 'admin-one', detail: 'Replace the strap sensor', at: '2026-11-26T10:00:00Z' }],
  ...over,
});

/** The ordinary routes an administrator's page needs; `extra` answers first. */
function careServer(me: AdminMe, history: () => Instruction[], extra?: (method: string, path: string, body: unknown, auth: string) => Answer | undefined) {
  return server((method, path, body, auth) => {
    const e = extra?.(method, path, body, auth);
    if (e) return e;
    if (path === '/api/v1/care/deployment-admin/me') return { status: 200, body: me };
    if (path === '/api/v1/workspaces') return { status: 200, body: { workspaces: [], count: 0, tenant_id: TENANT, single: true, edition: 'cloud' } };
    if (path.startsWith('/api/v1/care/people?')) return { status: 200, body: { people: [{ id: 'p1', display_name: 'Ada Hartley', preferred_name: 'Ada', timezone: 'UTC', status: 'active' }] } };
    if (path === '/api/v1/care/people/p1/devices') return { status: 200, body: { devices: [{ id: DEV, platform: 'wearos', status: 'paired', battery_percent: 70, on_wrist: true, background_sensor_permission: true, last_sync_at: '2026-11-26T09:00:00Z' }, { id: '44444444-4444-4444-8444-444444444444', platform: 'wearos', status: 'revoked', battery_percent: null, on_wrist: null, background_sensor_permission: null, last_sync_at: null }], count: 2 } };
    if (method === 'GET' && path.startsWith('/api/v1/care/deployment-admin/device-instructions?')) return { status: 200, body: { instructions: history() } };
    if (path === '/api/v1/care/ops/status') return { status: 200, body: { monitoring_paused: false, stale_after_s: 300, checked_at: '', jobs: {} } };
    return undefined;
  });
}

const signedIn = (accessToken = 'tok-1') => saveSession({ baseUrl: BASE, accessToken, refreshToken: 'refresh-1', workspaceId: WS, role: 'member', workspaceName: 'Lowbridge Care', username: 'ada.admin@example.com' });

async function openWatch() {
  const out = render(<DeviceAdminPage />);
  const watchField = await screen.findByLabelText('Watch id');
  await waitFor(() => expect((screen.getByLabelText('Tenant id') as HTMLInputElement).value).toBe(TENANT));
  fireEvent.change(watchField, { target: { value: DEV } });
  await screen.findByTestId('live');
  return out;
}

beforeEach(() => { forgetAdmins(); window.sessionStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

// ── Pure helpers ────────────────────────────────────────────────────────────────

test('an identifier is eight-four-four-four-twelve hex digits, nothing else', () => {
  expect(isUuid(TENANT)).toBe(true);
  expect(isUuid(TENANT.toUpperCase())).toBe(true);
  expect(isUuid(` ${TENANT} `)).toBe(true);
  for (const bad of ['', 'abc', TENANT.slice(0, -1), TENANT.replace('-', ''), TENANT.replace('1', 'g'), `${TENANT}1`]) expect(isUuid(bad)).toBe(false);
  expect(targetProblems({ tenantId: TENANT, workspaceId: 'x', deviceId: '' })).toEqual(['The workspace id is not complete.', 'The watch id is not complete.']);
  expect(targetProblems({ tenantId: TENANT, workspaceId: WS, deviceId: DEV })).toEqual([]);
});

test('a refusal is read as "confirm it is you" only when the server names step_up_required, in either shape', () => {
  expect(classifyRefusal(403, { detail: { code: 'step_up_required', message: 'Confirm who you are.', max_age_seconds: 300, auth_age_seconds: 900 } })).toBe('step_up');
  expect(classifyRefusal(403, { detail: 'Recent authentication needed.', error: 'step_up_required' })).toBe('step_up');
  expect(classifyRefusal(403, { detail: 'Recent authentication needed.', code: 'step_up_required' })).toBe('step_up');
  expect(classifyRefusal(403, undefined, 'step_up_required')).toBe('step_up');
  expect(classifyRefusal(403, { detail: 'This needs a deployment administrator.' })).toBe('not_admin');
  expect(classifyRefusal(403, { detail: 'You cannot approve your own request.' })).toBe('not_admin');
  expect(classifyRefusal(409, { detail: 'step_up_required' })).toBe('other');
  expect(classifyRefusal(500, { detail: { code: 'step_up_required' } })).toBe('other');
  expect(refusalMessage({ detail: { code: 'step_up_required', message: 'Confirm who you are.' } }, 'x')).toBe('Confirm who you are.');
  expect(refusalMessage({ detail: 'Plain.' }, 'x')).toBe('Plain.');
  expect(refusalMessage(undefined, 'fallback')).toBe('fallback');
});

test('every status word the server can send has words of its own, and the form checks its bounds', () => {
  const statuses = ['signed', 'delivered', 'applied', 'restored', 'refused', 'revoked', 'awaiting_approval', 'lapsed', 'withdrawn'];
  expect(Object.keys(STATUS_WORDS).sort()).toEqual([...statuses].sort());
  for (const s of statuses) expect(statusWords(s)).not.toContain('_');
  expect(statusWords('awaiting_approval')).toBe('Waiting for a second administrator');
  expect(statusWords('something_new')).toBe('something new');
  expect(issueProblems('MAINTENANCE_WINDOW', 10, 'Replace the strap sensor')).toEqual([]);
  expect(issueProblems('MAINTENANCE_WINDOW', 0, 'Replace the strap sensor')).toEqual(['Minutes must be a whole number from 1 to 15.']);
  expect(issueProblems('MAINTENANCE_WINDOW', 16, 'Replace the strap sensor')).toHaveLength(1);
  expect(issueProblems('MAINTENANCE_WINDOW', 2.5, 'Replace the strap sensor')).toHaveLength(1);
  expect(issueProblems('REBOOT', 10, 'short')).toEqual(['Choose one of the three actions.', 'Say why, in at least 10 characters.']);
  expect(stepUpWords({ ...ME_ADMIN, step_up_required: true, mfa_enrolled: true })).toContain('authenticator code');
  expect(stepUpWords({ ...ME_ADMIN, step_up_required: true })).not.toContain('authenticator code');
  expect(stepUpWords(ME_ADMIN)).toContain('20 s ago');
});

test('the step-up line keeps counting while the page is open, and says the next action will ask once it is too old', () => {
  // Round 10: a page opened 20 s after sign-in still said "20 s ago" minutes later.
  expect(stepUpWords(ME_ADMIN, 25)).toContain('45 s ago');
  expect(stepUpWords(ME_ADMIN, 200)).toContain('4 min ago');
  const stale = stepUpWords(ME_ADMIN, 280);   // 20 + 280 = 300 s, the server's limit
  expect(stale).toContain('you will be asked to confirm who you are');
  expect(stale).not.toContain('ago');
  // negative control: no time passing changes nothing, and a negative elapsed time is ignored
  expect(stepUpWords(ME_ADMIN, 0)).toBe(stepUpWords(ME_ADMIN));
  expect(stepUpWords(ME_ADMIN, -50)).toContain('20 s ago');
});

// ── The client's step-up call ───────────────────────────────────────────────────

test('confirming who you are replaces the sign-in through the renewal path and sends the code only when given', async () => {
  const calls = server((method, path, body) => {
    if (path === '/auth/step-up') return (body as { password: string }).password === 'right' ? { status: 200, body: { access_token: 'tok-2', token_type: 'bearer', expires_in: 900, auth_time: 1, amr: ['pwd'] } } : { status: 401, body: { detail: 'Wrong.' } };
    if (path.startsWith('/api/v1/care/people')) return { status: 200, body: { people: [] } };
    return undefined;
  });
  const api = new Api({ baseUrl: BASE, accessToken: 'tok-1', refreshToken: 'r', workspaceId: WS, role: 'member' });
  const told: string[] = [];
  api.onRenewed = (t) => told.push(t);
  await expect(api.stepUp('right')).resolves.toMatchObject({ access_token: 'tok-2' });
  expect(calls[0]).toMatchObject({ method: 'POST', path: '/auth/step-up', auth: 'Bearer tok-1', body: { password: 'right' } });
  expect('totp_code' in (calls[0].body as object)).toBe(false);
  expect(told).toEqual(['tok-2']);
  await api.people();
  expect(calls.at(-1)?.auth).toBe('Bearer tok-2');
  await api.stepUp('right', '123456');
  expect(calls.at(-1)?.body).toEqual({ password: 'right', totp_code: '123456' });
  await expect(api.stepUp('wrong')).rejects.toMatchObject({ status: 401, message: 'Wrong password or code.' });
  expect(told).toEqual(['tok-2', 'tok-2']);
});

test('a sign-in service that is unavailable is named, and no new sign-in is kept', async () => {
  server((_m, path) => (path === '/auth/step-up' ? { status: 503, body: { detail: 'down' } } : undefined));
  const api = new Api({ baseUrl: BASE, accessToken: 'tok-1', workspaceId: WS, role: 'member' });
  const told: string[] = [];
  api.onRenewed = (t) => told.push(t);
  await expect(api.stepUp('pw')).rejects.toMatchObject({ status: 503, message: expect.stringContaining('unavailable') });
  expect(told).toEqual([]);
});

// ── Refusal ─────────────────────────────────────────────────────────────────────

test('someone who is not a deployment administrator sees the refusal and nothing about watches or actions', async () => {
  signedIn();
  const calls = careServer(ME_NOT, () => [row()]);
  const { container } = render(<DeviceAdminPage />);
  expect(await screen.findByTestId('not-admin')).toHaveTextContent('This area is for deployment administrators.');
  expect(screen.getByRole('link', { name: /Back/ }).getAttribute('href')).toBe('/');
  expect(screen.queryByTestId('device-admin')).toBeNull();
  expect(screen.queryAllByRole('textbox')).toHaveLength(0);
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  expect(document.body.textContent).not.toMatch(/maintenance|factory|Replace the strap/i);
  // Nothing but the one question was asked of the server.
  expect(calls.map((c) => c.path)).toEqual(['/api/v1/care/deployment-admin/me']);
  expect(await axe(container)).toHaveNoViolations();
});

test('a server that cannot say who you are names the problem and shows no device controls', async () => {
  signedIn();
  careServer(ME_ADMIN, () => [], (_m, path) => (path.endsWith('/deployment-admin/me') ? { status: 500, body: { detail: 'Database away.' } } : undefined));
  render(<DeviceAdminPage />);
  expect(await screen.findByTestId('me-error')).toHaveTextContent('Could not check whether you are a deployment administrator: Database away.');
  expect(screen.queryByTestId('device-admin')).toBeNull();
  expect(screen.queryByTestId('not-admin')).toBeNull();
});

// ── The administrator's screen ──────────────────────────────────────────────────

test('an administrator sees the history with the server\'s status words, who did what, and the audit trail', async () => {
  signedIn();
  careServer(ME_ADMIN, () => [
    row(),
    row({ id: 'i2', action: 'FACTORY_RESET', state: 'pending_approval', status: 'awaiting_approval', issued_at: null, expires_at: null, requested_at: '2026-11-26T10:05:00Z', approval_deadline: '2026-11-26T10:35:00Z', issued_by: 'admin-one', reason: 'Watch is being retired', events: [{ event: 'requested', actor_kind: 'deployment_admin', actor_id: 'admin-one', detail: 'Watch is being retired', at: '2026-11-26T10:05:00Z' }] }),
    row({ id: 'i3', status: 'revoked', revoked_at: '2026-11-26T10:06:00Z', revoked_by: 'admin-two', events: [{ event: 'issued', actor_kind: 'deployment_admin', actor_id: 'admin-one', detail: '', at: '2026-11-26T10:00:00Z' }, { event: 'revoked', actor_kind: 'deployment_admin', actor_id: 'admin-two', detail: 'Wrong watch', at: '2026-11-26T10:06:00Z' }] }),
    row({ id: 'i4', status: 'applied', outcome: 'applied', outcome_at: '2026-11-26T10:02:00Z', delivered_at: '2026-11-26T10:01:00Z' }),
  ]);
  const { container } = await openWatch();
  const table = await screen.findByTestId('instructions');
  const statuses = within(table).getAllByTestId('status').map((s) => s.textContent);
  expect(statuses).toEqual(['Signed, not yet collected', 'Waiting for a second administrator', 'Revoked', 'Applied by the watch']);
  expect(table.textContent).not.toMatch(/awaiting_approval|MAINTENANCE_WINDOW|FACTORY_RESET/);
  const rows = within(table).getAllByTestId('instruction-row');
  expect(rows[0].textContent).toContain('by admin-one');
  expect(rows[2].textContent).toContain('by admin-two');
  expect(rows[1].textContent).toContain('approve by');
  // The audit trail per row, in a person's words.
  const trail = within(rows[2]).getByTestId('audit-trail');
  expect(trail.textContent).toContain('Issued');
  expect(trail.textContent).toContain('Revoked · deployment_admin admin-two · Wrong watch');
  expect(within(rows[2]).getByText('2 events')).toBeInTheDocument();
  // Approve only on a request waiting; revoke only on live rows.
  expect(within(rows[1]).getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  expect(within(rows[0]).queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(within(rows[0]).getByRole('button', { name: 'Revoke' })).toBeInTheDocument();
  expect(within(rows[2]).queryByRole('button', { name: 'Revoke' })).toBeNull();
  expect(screen.getByTestId('step-up-state')).toHaveTextContent('You confirmed who you are 20 s ago');
  expect(screen.getByTestId('live')).toHaveTextContent(/^Live: updated/);
  expect(await axe(container)).toHaveNoViolations();
});

test('an incomplete identifier is named and no history is asked for', async () => {
  signedIn();
  const calls = careServer(ME_ADMIN, () => [row()]);
  render(<DeviceAdminPage />);
  const watchField = await screen.findByLabelText('Watch id');
  fireEvent.change(watchField, { target: { value: 'not-an-identifier' } });
  expect(screen.getByTestId('target-problems')).toHaveTextContent('The watch id is not complete.');
  expect(watchField.getAttribute('aria-invalid')).toBe('true');
  expect(screen.queryByTestId('live')).toBeNull();
  expect(calls.some((c) => c.path.includes('device-instructions'))).toBe(false);
  expect(screen.getByRole('button', { name: 'Send to the watch' })).toBeDisabled();
});

test('choosing a person lists their paired watches and fills in the chosen one', async () => {
  signedIn();
  careServer(ME_ADMIN, () => []);
  render(<DeviceAdminPage />);
  const person = await screen.findByLabelText('Or choose a person');
  await waitFor(() => expect(person).not.toBeDisabled());
  fireEvent.change(person, { target: { value: 'p1' } });
  const picker = await screen.findByTestId('watch-picker');
  await waitFor(() => expect(picker).not.toBeDisabled());
  // The revoked watch is not offered.
  expect(within(picker).getAllByRole('option')).toHaveLength(2);
  fireEvent.change(picker, { target: { value: DEV } });
  expect((screen.getByLabelText('Watch id') as HTMLInputElement).value).toBe(DEV);
  expect(await screen.findByTestId('no-instructions')).toBeInTheDocument();
});

test('a maintenance window is sent with the chosen target and the new row appears', async () => {
  signedIn();
  const rows: Instruction[] = [];
  const calls = careServer(ME_ADMIN, () => rows, (method, path, body) => {
    if (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions') {
      const b = body as { action: string; minutes: number; reason: string };
      rows.push(row({ id: 'new', action: b.action, window_minutes: b.minutes, reason: b.reason }));
      return { status: 201, body: rows[rows.length - 1] };
    }
    return undefined;
  });
  await openWatch();
  expect(await screen.findByTestId('no-instructions')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Minutes'), { target: { value: '5' } });
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Replace the strap sensor' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send to the watch' }));
  expect(await screen.findByTestId('notice')).toHaveTextContent(/^Signed\./);
  const post = calls.find((c) => c.method === 'POST')!;
  expect(post.body).toEqual({ tenant_id: TENANT, workspace_id: WS, device_id: DEV, action: 'MAINTENANCE_WINDOW', minutes: 5, reason: 'Replace the strap sensor' });
  const table = await screen.findByTestId('instructions');
  expect(within(table).getAllByTestId('instruction-row')).toHaveLength(1);
  expect(table.textContent).toContain('Replace the strap sensor');
  expect(table.textContent).toContain('5 min');
  expect((screen.getByLabelText('Reason') as HTMLInputElement).value).toBe('');
});

test('a factory reset explains the two-person rule and a 202 says it waits for a second administrator', async () => {
  signedIn();
  careServer(ME_ADMIN, () => [], (method, path) => (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions'
    ? { status: 202, body: row({ id: 'req', action: 'FACTORY_RESET', state: 'pending_approval', status: 'awaiting_approval' }) } : undefined));
  await openWatch();
  expect(screen.queryByTestId('two-person')).toBeNull();
  fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'FACTORY_RESET' } });
  expect(screen.getByTestId('two-person')).toHaveTextContent('a different deployment administrator must approve the request within 30 minutes');
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Watch is being retired' } });
  fireEvent.click(screen.getByRole('button', { name: 'Request a factory reset' }));
  expect(await screen.findByTestId('notice')).toHaveTextContent('Waiting for a second administrator');
});

test('approving calls the approve route; a refusal to approve your own request is shown in the server\'s words', async () => {
  signedIn();
  const waiting = row({ id: 'req', action: 'FACTORY_RESET', state: 'pending_approval', status: 'awaiting_approval', issued_at: null, requested_at: '2026-11-26T10:05:00Z', approval_deadline: '2026-11-26T10:35:00Z' });
  let answer: Answer = { status: 200, body: { ...waiting, status: 'signed', state: 'signed' } };
  const calls = careServer(ME_ADMIN, () => [waiting], (method, path) => (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions/req/approve' ? answer : undefined));
  await openWatch();
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
  expect(await screen.findByTestId('notice')).toHaveTextContent('Approved and signed');
  expect(calls.find((c) => c.path.endsWith('/approve'))!.body).toEqual({ tenant_id: TENANT, workspace_id: WS });
  answer = { status: 403, body: { detail: 'The requester cannot approve their own factory reset.' } };
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  expect(await screen.findByTestId('action-error')).toHaveTextContent('The requester cannot approve their own factory reset.');
  expect(screen.queryByTestId('step-up')).toBeNull();
});

test('revoking asks why and sends the reason', async () => {
  signedIn();
  const calls = careServer(ME_ADMIN, () => [row()], (method, path) => (method === 'POST' && path.endsWith('/i1/revoke') ? { status: 200, body: row({ status: 'revoked' }) } : undefined));
  await openWatch();
  fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }));
  const confirm = screen.getByRole('button', { name: 'Confirm revoke' });
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Why revoke'), { target: { value: 'Wrong watch chosen' } });
  fireEvent.click(confirm);
  expect(await screen.findByTestId('notice')).toHaveTextContent('Revoked');
  expect(calls.find((c) => c.path.endsWith('/revoke'))!.body).toEqual({ tenant_id: TENANT, workspace_id: WS, reason: 'Wrong watch chosen' });
});

// ── Step-up ─────────────────────────────────────────────────────────────────────

const STEP_UP_403: Answer = { status: 403, body: { detail: { code: 'step_up_required', message: 'Please confirm who you are.', max_age_seconds: 300, auth_age_seconds: 4000 } } };

/** A server that refuses the first attempt until the sign-in is the stepped-up one. */
function stepUpServer(me: AdminMe, stepUpAnswer: (body: unknown) => Answer) {
  const rows: Instruction[] = [];
  const calls = careServer(me, () => rows, (method, path, body, auth) => {
    if (path === '/auth/step-up') return stepUpAnswer(body);
    if (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions') {
      if (auth !== 'Bearer tok-2') return STEP_UP_403;
      rows.push(row({ id: 'new', reason: (body as { reason: string }).reason }));
      return { status: 201, body: rows[0] };
    }
    return undefined;
  });
  return { calls, rows };
}
const issues = (calls: Call[]) => calls.filter((c) => c.method === 'POST' && c.path === '/api/v1/care/deployment-admin/device-instructions');

async function sendOne() {
  await openWatch();
  await screen.findByTestId('no-instructions');
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Replace the strap sensor' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send to the watch' }));
}

test('a refusal asking for confirmation opens the panel; the password is sent once, the new sign-in kept, and the action retried exactly once', async () => {
  signedIn();
  const { calls } = stepUpServer({ ...ME_ADMIN, step_up_required: true, auth_age_seconds: 4000 }, () => ({ status: 200, body: { access_token: 'tok-2', token_type: 'bearer', expires_in: 900, auth_time: 1, amr: ['pwd'] } }));
  await sendOne();
  const panel = await screen.findByTestId('step-up');
  expect(screen.getByTestId('step-up-state')).toHaveTextContent('you will be asked to confirm who you are with your password.');
  expect(within(panel).queryByLabelText('Authenticator code')).toBeNull();
  expect(screen.queryByTestId('notice')).toBeNull();
  const password = within(panel).getByLabelText('Password') as HTMLInputElement;
  fireEvent.change(password, { target: { value: 'correct horse' } });
  fireEvent.click(within(panel).getByRole('button', { name: 'Confirm and continue' }));
  expect(await screen.findByTestId('notice')).toHaveTextContent(/^Signed\./);
  const stepUp = calls.filter((c) => c.path === '/auth/step-up');
  expect(stepUp).toHaveLength(1);
  expect(stepUp[0].body).toEqual({ password: 'correct horse' });
  expect(stepUp[0].auth).toBe('Bearer tok-1');
  // Refused once, sent again once with the new sign-in, and never a third time.
  expect(issues(calls).map((c) => c.auth)).toEqual(['Bearer tok-1', 'Bearer tok-2']);
  // The stored session carries the new sign-in for the next page, and the panel is gone.
  expect(JSON.parse(window.sessionStorage.getItem('ira_care_dashboard_session')!).accessToken).toBe('tok-2');
  expect(screen.queryByTestId('step-up')).toBeNull();
  expect(screen.queryByText('correct horse')).toBeNull();
  expect(await screen.findByTestId('instructions')).toBeInTheDocument();
});

test('with an authenticator enrolled the code is asked for and sent with the password', async () => {
  signedIn();
  const { calls } = stepUpServer({ ...ME_ADMIN, step_up_required: true, mfa_enrolled: true }, () => ({ status: 200, body: { access_token: 'tok-2', token_type: 'bearer', expires_in: 900, auth_time: 1, amr: ['pwd', 'otp'] } }));
  await sendOne();
  const panel = await screen.findByTestId('step-up');
  const confirm = within(panel).getByRole('button', { name: 'Confirm and continue' });
  fireEvent.change(within(panel).getByLabelText('Password'), { target: { value: 'correct horse' } });
  expect(confirm).toBeDisabled();
  fireEvent.change(within(panel).getByLabelText('Authenticator code'), { target: { value: '246810' } });
  fireEvent.click(confirm);
  expect(await screen.findByTestId('notice')).toHaveTextContent(/^Signed\./);
  expect(calls.find((c) => c.path === '/auth/step-up')!.body).toEqual({ password: 'correct horse', totp_code: '246810' });
  expect(issues(calls)).toHaveLength(2);
});

test('a wrong password shows the refusal, keeps the old sign-in and does not retry the action', async () => {
  signedIn();
  const { calls } = stepUpServer({ ...ME_ADMIN, step_up_required: true }, () => ({ status: 401, body: { detail: 'Wrong.' } }));
  await sendOne();
  const panel = await screen.findByTestId('step-up');
  fireEvent.change(within(panel).getByLabelText('Password'), { target: { value: 'nope' } });
  fireEvent.click(within(panel).getByRole('button', { name: 'Confirm and continue' }));
  expect(await screen.findByTestId('step-up-error')).toHaveTextContent('Wrong password or code.');
  expect(issues(calls)).toHaveLength(1);
  expect(JSON.parse(window.sessionStorage.getItem('ira_care_dashboard_session')!).accessToken).toBe('tok-1');
  expect(screen.getByTestId('step-up')).toBeInTheDocument();
  expect((within(panel).getByLabelText('Password') as HTMLInputElement).value).toBe('');
  expect(screen.queryByTestId('notice')).toBeNull();
});

test('a refusal that does not ask for confirmation is shown and opens no panel; cancelling the panel sends nothing', async () => {
  signedIn();
  let answer: Answer = { status: 403, body: { detail: 'This needs a deployment administrator.' } };
  const calls = careServer(ME_ADMIN, () => [], (method, path) => (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions' ? answer : undefined));
  await sendOne();
  expect(await screen.findByTestId('action-error')).toHaveTextContent('This needs a deployment administrator.');
  expect(screen.queryByTestId('step-up')).toBeNull();
  answer = STEP_UP_403;
  fireEvent.click(screen.getByRole('button', { name: 'Send to the watch' }));
  const panel = await screen.findByTestId('step-up');
  fireEvent.click(within(panel).getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByTestId('step-up')).toBeNull();
  expect(screen.getByTestId('action-error')).toHaveTextContent('Nothing was sent');
  expect(calls.filter((c) => c.path === '/auth/step-up')).toHaveLength(0);
  expect(issues(calls)).toHaveLength(2);
});

test('a confirmation that is still refused is not retried again', async () => {
  signedIn();
  const calls = careServer({ ...ME_ADMIN, step_up_required: true }, () => [], (method, path) => {
    if (path === '/auth/step-up') return { status: 200, body: { access_token: 'tok-2', token_type: 'bearer', expires_in: 900, auth_time: 1, amr: ['pwd'] } };
    if (method === 'POST' && path === '/api/v1/care/deployment-admin/device-instructions') return STEP_UP_403;
    return undefined;
  });
  await sendOne();
  const panel = await screen.findByTestId('step-up');
  fireEvent.change(within(panel).getByLabelText('Password'), { target: { value: 'pw' } });
  fireEvent.click(within(panel).getByRole('button', { name: 'Confirm and continue' }));
  expect(await screen.findByTestId('action-error')).toHaveTextContent('refused again');
  expect(issues(calls)).toHaveLength(2);
  expect(screen.queryByTestId('step-up')).toBeNull();
});

// ── Live ────────────────────────────────────────────────────────────────────────

test('the history refreshes every 30 seconds, says when it last updated, and says when it is out of date', async () => {
  signedIn();
  let down = false;
  const calls = careServer(ME_ADMIN, () => [row()], (method, path) => (down && method === 'GET' && path.includes('device-instructions') ? { status: 500, body: {} } : undefined));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  await openWatch();
  await screen.findByTestId('instructions');
  expect(screen.getByTestId('live')).toHaveTextContent(/^Live: updated/);
  const history = () => calls.filter((c) => c.method === 'GET' && c.path.includes('device-instructions')).length;
  const before = history();
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(history()).toBeGreaterThan(before);
  down = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  await waitFor(() => expect(screen.getByTestId('live')).toHaveTextContent(/^Connection lost: reconnecting/));
  expect(screen.getByTestId('history-error')).toHaveTextContent('The history could not be loaded');
  // What was last seen still shows.
  expect(screen.getByTestId('instructions')).toBeInTheDocument();
});

// ── Navigation ──────────────────────────────────────────────────────────────────

test('the navigation shows the deployment link to administrators only, asking once', async () => {
  signedIn('tok-not');
  const calls = careServer(ME_NOT, () => []);
  const first = render(<TopBar />);
  await waitFor(() => expect(screen.getByText('Sign out')).toBeInTheDocument());
  await waitFor(() => expect(calls.some((c) => c.path.endsWith('/deployment-admin/me'))).toBe(true));
  expect(screen.queryByTestId('nav-deployment')).toBeNull();
  first.unmount();
  render(<TopBar />);
  await waitFor(() => expect(screen.getByText('Sign out')).toBeInTheDocument());
  expect(screen.queryByTestId('nav-deployment')).toBeNull();
  expect(calls.filter((c) => c.path.endsWith('/deployment-admin/me'))).toHaveLength(1);
  // Nothing about it in storage: the answer lives in memory only.
  expect(JSON.stringify(window.sessionStorage)).not.toContain('deployment');
  for (let i = 0; i < window.sessionStorage.length; i++) expect(window.sessionStorage.getItem(window.sessionStorage.key(i)!)).not.toContain('deployment_admin');
});

test('an administrator sees the deployment link', async () => {
  signedIn('tok-admin');
  careServer(ME_ADMIN, () => []);
  render(<TopBar />);
  expect(await screen.findByTestId('nav-deployment')).toHaveAttribute('href', '/admin/devices');
});
