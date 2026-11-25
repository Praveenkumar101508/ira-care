import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import type { Permissions } from '@/lib/types';

let tab = 'plan';
vi.mock('next/navigation', () => ({ useParams: () => ({ id: 'p1' }), useSearchParams: () => new URLSearchParams(`tab=${tab}`) }));
vi.mock('next/link', () => ({ default: ({ children, href }: { children: unknown; href: string }) => <a href={href}>{children as never}</a> }));

const level = (role: string): Permissions => {
  const act = role !== 'viewer'; const manage = role === 'workspace_admin';
  return {
    role, label: { workspace_admin: 'Care manager', member: 'Carer', viewer: 'Viewer' }[role] ?? 'No access', who: 'Who text.',
    sees_everyone: manage, can_read: true, can_act: act, can_manage: manage,
    actions: { view_care_plan: true, edit_care_plan: manage, record_visits: act, acknowledge_alerts: act },
  };
};
let perms: Permissions | 'fail' = level('viewer');
const soon = new Date(Date.now() + 3600_000).toISOString();
const fakeApi = {
  status: vi.fn(async () => ({ person: { id: 'p1', display_name: 'Ada', preferred_name: 'Ada', timezone: 'UTC', status: 'active', notes: '', home_address: '', home_latitude: null, home_longitude: null, home_radius_m: 150, emergency_summary: '', has_access_notes: false },
    latest_readings: [], open_alerts: [], last_checkin: null, devices: [] })),
  alerts: vi.fn(async () => []), checkins: vi.fn(async () => ({ checkins: [], answers_included: false })), vitals: vi.fn(async () => []),
  routines: vi.fn(async () => []), medications: vi.fn(async () => []), medicationEvents: vi.fn(async () => []), circle: vi.fn(async () => []),
  settings: vi.fn(async () => null), consents: vi.fn(async () => ({ consents: [] })), memory: vi.fn(async () => []),
  profile: vi.fn(async () => null), reminders: vi.fn(async () => []), transcripts: vi.fn(async () => []),
  visits: vi.fn(async () => [{ id: 'v1', person_id: 'p1', status: 'planned', planned_start: soon, planned_end: soon, carer_name: 'Sravani', purpose: '', outcome: '', handover: '', notes: '', tasks: [] }]),
  permissions: vi.fn(async () => { if (perms === 'fail') throw new Error('503'); return perms; }),
  carePlan: vi.fn(async () => ({ id: 'c', person_id: 'p1', version: 1, goals: 'Stay at home', support: '', routine: [], risks: '', emergency: '', review_on: null, reason: 'First', author_user_id: null, author_name: 'M', created_at: '2026-11-25T10:00:00Z' })),
  carePlanHistory: vi.fn(async () => []),
};
vi.mock('@/components/useApi', () => ({ useApi: () => ({ api: fakeApi, session: { role: 'workspace_admin' }, ready: true }), canManage: () => true }));

import PersonPage from '@/app/people/[id]/page';

test.each([
  ['viewer', false], ['member', false], ['workspace_admin', true],
])('the care plan tab: a %s may change the plan: %s', async (role, edits) => {
  tab = 'plan'; perms = level(role);
  render(<PersonPage />);
  expect(await screen.findByTestId('plan-v1')).toHaveTextContent('Stay at home');
  expect(await screen.findByTestId('staff-role')).toHaveTextContent(`You are signed in as ${level(role).label}.`);
  expect(!!screen.queryByText('Change the plan')).toBe(edits);
});

test('a viewer is not offered visit actions; a carer is', async () => {
  tab = 'care'; perms = level('viewer');
  const { unmount } = render(<PersonPage />);
  await screen.findAllByText(/Sravani/);
  expect(screen.queryByText('Carer has arrived')).toBeNull();
  unmount();
  perms = level('member');
  render(<PersonPage />);
  expect(await screen.findByText('Carer has arrived')).toBeInTheDocument();
});

test('without an answer about permissions nothing that changes the record is offered, whatever the stored session says', async () => {
  tab = 'plan'; perms = 'fail';
  render(<PersonPage />);
  await screen.findByTestId('plan-v1');
  expect(screen.queryByText('Change the plan')).toBeNull();
  expect(screen.queryByRole('link', { name: 'Access' })).toBeNull();
});
