import type { Alert, Person, Reminder, Visit } from '../api/types';
import { alertBadge, nextVisit, nextVisitWords, openReminders, pickPerson, todaysVisits, HOME_CARDS } from '../model/experience';

const person = (id: string): Person => ({ id, display_name: id, preferred_name: '', timezone: 'UTC', status: 'active' });
const visit = (id: string, start: string, status: Visit['status'] = 'planned'): Visit => ({
  id, person_id: 'p', carer_name: 'Sravani', planned_start: start, planned_end: start.replace('T10', 'T11'), purpose: '', status,
  arrived_at: null, departed_at: null, outcome: '', notes: '', handover: '', tasks: [],
});
const now = new Date('2026-11-24T09:00:00Z');

test('the account starts on the remembered person when still theirs, else the first they belong to', () => {
  const people = [person('a'), person('b'), person('c')];
  const mem = [{ id: 'm', person_id: 'b', person_name: 'b', role: 'family', relationship: 'daughter', display_name: 'S', tier: 1 },
               { id: 'n', person_id: 'c', person_name: 'c', role: 'family', relationship: 'daughter', display_name: 'S', tier: 1 }];
  expect(pickPerson(people, mem, 'c')?.id).toBe('c');
  expect(pickPerson(people, mem, 'a')?.id).toBe('b');
  expect(pickPerson(people, [], null)?.id).toBe('a');
});

test('the next visit is the one in progress, else the next planned within a day, in plain words', () => {
  const later = visit('v2', '2026-11-24T14:00:00+00:00');
  const soon = visit('v1', '2026-11-24T10:00:00+00:00');
  const far = visit('v3', '2026-11-27T10:00:00+00:00');
  expect(nextVisit([later, soon, far], now)?.id).toBe('v1');
  expect(nextVisit([later, { ...soon, status: 'arrived' }], now)?.id).toBe('v1');
  expect(nextVisitWords(nextVisit([far], now))).toBe('No visit planned in the next day.');
  expect(nextVisitWords(soon)).toBe('Sravani is coming at 10:00.');
  expect(nextVisitWords({ ...soon, status: 'arrived' })).toBe('Sravani is here.');
});

test("a carer's today list is today's visits in order, without cancelled ones", () => {
  const list = todaysVisits([visit('b', '2026-11-24T14:00:00+00:00'), visit('a', '2026-11-24T10:00:00+00:00'), visit('x', '2026-11-24T12:00:00+00:00', 'cancelled'), visit('y', '2026-11-25T10:00:00+00:00')], now);
  expect(list.map((v) => v.id)).toEqual(['a', 'b']);
});

test('open reminders come soonest first; the alerts badge counts open alerts for my people', () => {
  const r = (id: string, due: string, status: Reminder['status']): Reminder => ({ id, person_id: 'p', kind: 'reminder', text: id, medication_id: null, due_at: due, status, snoozed_until: null, source: 'carer', asked_count: 0, done_at: null });
  expect(openReminders([r('late', '2026-11-24T18:00:00Z', 'pending'), r('done', '2026-11-24T08:00:00Z', 'confirmed'), r('soon', '2026-11-24T10:00:00Z', 'snoozed')]).map((x) => x.id)).toEqual(['soon', 'late']);
  const a = (id: string, pid: string, status: Alert['status']): Alert => ({ id, person_id: pid, kind: 'help_requested', severity: 'sos', status, opened_at: '2026-11-24T09:00:00Z', current_tier: 1, acknowledged_at: null, resolved_at: null, details: {} });
  const mem = [{ id: 'm', person_id: 'p1', person_name: 'p1', role: 'family', relationship: '', display_name: 'S', tier: 1 }];
  expect(alertBadge([a('1', 'p1', 'open'), a('2', 'p2', 'open'), a('3', 'p1', 'resolved')], mem)).toBe(1);
  expect(alertBadge([a('1', 'p1', 'open'), a('2', 'p2', 'escalating')], [])).toBe(2);
});

test('every experience has Help or Alerts first, so the urgent action is never buried by default', () => {
  expect(HOME_CARDS.wearer[0].key).toBe('help');
  expect(HOME_CARDS.family[0].key).toBe('alerts');
  expect(HOME_CARDS.carer[0].key).toBe('alerts');
});
