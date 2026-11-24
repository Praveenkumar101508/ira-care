import type { Visit } from '../api/types';
import { currentVisit, visitActions, visitTitle, VISIT_OUTCOME_WORDS, VISIT_WORDS } from '../model/visits';

const base: Visit = {
  id: 'v1', person_id: 'p', carer_name: 'Sravani', planned_start: '2026-11-23T10:00:00+00:00', planned_end: '2026-11-23T11:00:00+00:00',
  purpose: 'Morning visit', status: 'planned', arrived_at: null, departed_at: null, outcome: '', notes: '', handover: '', tasks: [],
};
const now = new Date('2026-11-23T09:00:00Z');

test('the carer acts on the visit they are in, else the next open one', () => {
  const later = { ...base, id: 'v2', planned_start: '2026-11-23T14:00:00+00:00', planned_end: '2026-11-23T15:00:00+00:00' };
  const here = { ...base, id: 'v3', status: 'arrived' as const, arrived_at: '2026-11-23T10:05:00+00:00' };
  expect(currentVisit([later, base], now)?.id).toBe('v1');
  expect(currentVisit([later, base, here], now)?.id).toBe('v3');
  expect(currentVisit([{ ...base, status: 'departed' }], now)).toBeUndefined();
  const old = { ...base, id: 'old', planned_start: '2026-11-22T10:00:00+00:00', planned_end: '2026-11-22T11:00:00+00:00' };
  expect(currentVisit([old], now)).toBeUndefined();
});

test('arrive before, depart during, nothing after', () => {
  expect(visitActions(base)).toEqual(['arrive']);
  expect(visitActions({ ...base, status: 'late' })).toEqual(['arrive']);
  expect(visitActions({ ...base, status: 'missed' })).toEqual(['arrive']);
  expect(visitActions({ ...base, status: 'arrived' })).toEqual(['depart']);
  expect(visitActions({ ...base, status: 'departed' })).toEqual([]);
  expect(visitActions({ ...base, status: 'cancelled' })).toEqual([]);
});

test('visit words describe what is planned or what happened, never judge', () => {
  expect(visitTitle(base)).toBe('Sravani · 10:00 to 11:00 · Planned');
  expect(visitTitle({ ...base, carer_name: '' })).toMatch(/^A carer/);
  for (const w of [...Object.values(VISIT_WORDS), ...Object.values(VISIT_OUTCOME_WORDS)]) {
    expect(w.toLowerCase()).not.toMatch(/fail|neglig|diagnos|fault/);
  }
});
