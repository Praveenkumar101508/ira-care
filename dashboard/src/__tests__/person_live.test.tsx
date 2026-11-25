import { act, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

let personId = 'p1';
vi.mock('next/navigation', () => ({
  useParams: () => ({ id: personId }),
  useSearchParams: () => new URLSearchParams('tab=overview'),
}));
vi.mock('next/link', () => ({ default: ({ children, href }: { children: unknown; href: string }) => <a href={href}>{children as never}</a> }));

const person = (id: string, name: string) => ({
  id, display_name: name, preferred_name: name, timezone: 'UTC', status: 'active', notes: '', home_address: '', home_latitude: null,
  home_longitude: null, home_radius_m: 150, emergency_summary: '', has_access_notes: false,
});
let onWrist: boolean | null = null;
let failConsents = false;
const statusCalls: string[] = [];
const fakeApi = {
  status: vi.fn(async (id: string) => {
    statusCalls.push(id);
    return {
      person: person(id, id === 'p1' ? 'Ada' : 'Bea'), latest_readings: [], open_alerts: [], last_checkin: null,
      devices: [{ id: 'd', platform: 'wearos', status: 'paired', battery_percent: 80, on_wrist: onWrist, background_sensor_permission: true, last_sync_at: new Date().toISOString() }],
    };
  }),
  opsStatus: vi.fn(async () => ({ monitoring_paused: false, delivery: { delivery_problem: false } })),
  alerts: vi.fn(async () => []), checkins: vi.fn(async () => ({ checkins: [], answers_included: false })), vitals: vi.fn(async () => []),
  routines: vi.fn(async () => []), medications: vi.fn(async () => []), medicationEvents: vi.fn(async () => []), circle: vi.fn(async () => []),
  settings: vi.fn(async () => null), consents: vi.fn(async () => { if (failConsents) throw new Error('503'); return { consents: [] }; }),
  visits: vi.fn(async () => []), memory: vi.fn(async () => []), profile: vi.fn(async () => null), reminders: vi.fn(async () => []),
  transcripts: vi.fn(async () => []),
};
vi.mock('@/components/useApi', () => ({ useApi: () => ({ api: fakeApi, session: { role: 'member' }, ready: true }), canManage: () => false }));

import PersonPage from '@/app/people/[id]/page';

beforeEach(() => { statusCalls.length = 0; onWrist = null; failConsents = false; personId = 'p1'; vi.useFakeTimers({ shouldAdvanceTime: true }); });
afterEach(() => { vi.useRealTimers(); });

test('an unknown wrist state is shown as unknown, never as on the wrist', async () => {
  render(<PersonPage />);
  expect(await screen.findByTestId('headline')).toHaveTextContent('Needs a look: not known whether the watch is worn');
  expect(document.body.textContent).toContain('wrist unknown');
  expect(document.body.textContent).not.toContain('on the wrist');
});

test('the record refreshes itself every 30 seconds and says when it last updated', async () => {
  onWrist = true;
  render(<PersonPage />);
  await screen.findByTestId('headline');
  expect(screen.getByTestId('live')).toHaveTextContent(/^Live: updated/);
  const before = statusCalls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(statusCalls.length).toBeGreaterThan(before);
});

test('one section failing is named and the rest of the record still shows', async () => {
  onWrist = true;
  failConsents = true;
  render(<PersonPage />);
  expect(await screen.findByTestId('headline')).toBeInTheDocument();
  expect(screen.getByTestId('live')).toHaveTextContent('Some sections could not be loaded (consents)');
});

test('a health check that fails withholds All well, and the record still loads', async () => {
  onWrist = true;
  fakeApi.opsStatus.mockImplementationOnce(async () => { throw new Error('down'); });
  render(<PersonPage />);
  const h = await screen.findByTestId('headline');
  expect(screen.getByTestId('live')).toHaveTextContent(/^Live: updated/);
  expect(h).not.toHaveTextContent(/^All well/);
  expect(h).toHaveTextContent(/Cannot confirm all is well/);
});

test('with the system healthy and nothing wrong, the headline says All well', async () => {
  onWrist = true;
  render(<PersonPage />);
  const h = await screen.findByTestId('headline');
  await waitFor(() => expect(h).toHaveTextContent(/^All well/));
});
