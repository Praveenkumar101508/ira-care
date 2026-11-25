import { fireEvent, render, screen, within } from '@testing-library/react';
import { vi } from 'vitest';
import { CaseloadList, SearchResults, VisitPlan } from '@/components/Team';
import { byDayPlan, hitHref, needsAttention, searchable } from '@/lib/team';
import type { Caseload, PlannedVisit, SearchHit, StaffMember } from '@/lib/types';

const visit = (id: string, start: string, extra: Partial<PlannedVisit> = {}): PlannedVisit => ({
  id, person_id: `p-${id}`, person_name: `Person ${id}`, planned_start: start, planned_end: start, status: 'planned',
  purpose: 'Lunch', carer_name: 'Kim', carer_member_id: 'm1', ...extra,
});

test('a search result always leads to the person\'s own record, on the matching tab', () => {
  const hit = (kind: SearchHit['kind']): SearchHit => ({ kind, id: 'x', person_id: 'p 1', person_name: 'Ada', text: 't', at: null });
  expect(hitHref(hit('person'))).toBe('/people/p%201?tab=overview');
  expect(hitHref(hit('visit'))).toBe('/people/p%201?tab=care');
  expect(hitHref(hit('alert'))).toBe('/people/p%201?tab=alerts');
});

test('a query needs two letters, spaces aside', () => {
  expect(searchable(' a ')).toBe(false);
  expect(searchable('a  b')).toBe(true);
  expect(searchable('Ad')).toBe(true);
});

test('the plan is grouped by day in time order, and problems are named', () => {
  const groups = byDayPlan([visit('b', '2026-11-26T09:00:00Z'), visit('a', '2026-11-25T15:00:00Z'), visit('c', '2026-11-25T08:00:00Z')]);
  expect(groups.map((g) => g.day)).toEqual(['2026-11-25', '2026-11-26']);
  expect(groups[0].visits.map((v) => v.id)).toEqual(['c', 'a']);
  expect(needsAttention(visit('m', '2026-11-25T08:00:00Z', { status: 'missed' }))).toBe('Missed');
  expect(needsAttention(visit('l', '2026-11-25T08:00:00Z', { status: 'late' }))).toBe('Late');
  expect(needsAttention(visit('u', '2026-11-25T08:00:00Z', { carer_member_id: null, carer_name: '' }))).toBe('No carer assigned');
  expect(needsAttention(visit('ok', '2026-11-25T08:00:00Z'))).toBeNull();
});

test('search results show kind, person and the record line, linked to the record', () => {
  render(<SearchResults hits={[
    { kind: 'person', id: 'p1', person_id: 'p1', person_name: 'Ada Lovelace', text: 'Ada Lovelace', at: null },
    { kind: 'visit', id: 'v1', person_id: 'p1', person_name: 'Ada Lovelace', text: 'Lunch · Kim · planned', at: '2026-11-25T12:00:00Z' },
  ]} />);
  const links = screen.getAllByRole('link', { name: 'Ada Lovelace' });
  expect(links.map((l) => l.getAttribute('href'))).toEqual(['/people/p1?tab=overview', '/people/p1?tab=care']);
  expect(screen.getByTestId('search-results').textContent).toContain('Lunch · Kim · planned');
});

test('no results says so', () => {
  render(<SearchResults hits={[]} />);
  expect(screen.getByTestId('no-results')).toBeInTheDocument();
});

const caseload: Caseload = { everyone: true, carers: [
  { user_id: 'u1', name: 'Sam Carer', people: [{ person_id: 'p1', name: 'Ada', member_id: 'm1', next_visit: '2026-11-25T12:00:00Z' }] },
  { user_id: 'u2', name: 'Kim Carer', people: [{ person_id: 'p2', name: 'Bea', member_id: 'm2', next_visit: null }] },
] };
const staff: StaffMember[] = [{ user_id: 'u2', name: 'Kim Carer', roles: ['carer'], people: 1, coordinator: true }];

test('a care manager can name a coordinator from the caseload', () => {
  const onCoordinator = vi.fn();
  render(<CaseloadList caseload={caseload} staff={staff} canDesignate onCoordinator={onCoordinator} />);
  const kim = screen.getByRole('region', { name: 'Caseload of Kim Carer' });
  expect(kim.textContent).toContain('coordinator');
  expect(within(kim).getByText(/no visit planned/)).toBeInTheDocument();
  const sam = screen.getByRole('region', { name: 'Caseload of Sam Carer' });
  fireEvent.click(within(sam).getByRole('checkbox'));
  expect(onCoordinator).toHaveBeenCalledWith('u1', true);
});

test('anyone else sees the caseload and no coordinator switch', () => {
  render(<CaseloadList caseload={caseload} staff={[]} canDesignate={false} onCoordinator={vi.fn()} />);
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  expect(screen.getByRole('link', { name: 'Ada' }).getAttribute('href')).toBe('/people/p1');
});

test('the visit plan flags a missed visit', () => {
  render(<VisitPlan visits={[visit('m', '2026-11-25T08:00:00Z', { status: 'missed', person_name: 'Ada' })]} />);
  expect(screen.getByTestId('visit-plan').textContent).toContain('Missed');
});
