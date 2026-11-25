import type { Alert, Checkin, Reading } from '@/lib/types';
import { alertWords, byDay, checkinCounts, polyline, series, sortAlerts, timeAgo, worstOpenAlert } from '@/lib/view';

const base: Alert = {
  id: 'a', person_id: 'p1', kind: 'low_battery', severity: 'notice', status: 'open', opened_at: '2026-11-21T10:00:00Z',
  current_tier: 0, acknowledged_at: null, resolved_at: null, details: {},
};

test('alerts sort most serious and unacknowledged first', () => {
  const list = [
    { ...base, id: 'n' },
    { ...base, id: 's', severity: 'sos' as const, opened_at: '2026-11-21T10:05:00Z' },
    { ...base, id: 'u', severity: 'urgent' as const },
    { ...base, id: 'k', severity: 'sos' as const, status: 'acknowledged' as const },
  ];
  expect(sortAlerts(list).map((a) => a.id)).toEqual(['s', 'u', 'n', 'k']);
});

test('the overview shows each person their worst open alert only', () => {
  const list = [
    { ...base, id: 'x', person_id: 'p2', severity: 'sos' as const },
    { ...base, id: 'y', person_id: 'p1', severity: 'urgent' as const, status: 'acknowledged' as const },
    { ...base, id: 'z', person_id: 'p1' },
  ];
  expect(worstOpenAlert(list, 'p1')?.id).toBe('z');
  expect(worstOpenAlert(list, 'p2')?.id).toBe('x');
  expect(worstOpenAlert(list, 'p3')).toBeUndefined();
});

test('alert words describe, never diagnose', () => {
  for (const kind of Object.keys({ help_requested: 1, heart_rate_outside_range: 1, no_answer_no_movement: 1, missed_checkins: 1 })) {
    expect(alertWords({ ...base, kind }).toLowerCase()).not.toMatch(/diagnos|cardiac|stroke|arrhythm/);
  }
  expect(alertWords({ ...base, kind: 'brand_new' })).toBe('brand new');
});

test('time ago in plain words', () => {
  const now = new Date('2026-11-21T12:00:00Z');
  expect(timeAgo('2026-11-21T11:59:50Z', now)).toBe('just now');
  expect(timeAgo('2026-11-21T11:30:00Z', now)).toBe('30 min ago');
  expect(timeAgo('2026-11-20T12:00:00Z', now)).toBe('24 h ago');
  expect(timeAgo('2026-11-10T12:00:00Z', now)).toBe('11 days ago');
  expect(timeAgo(null, now)).toBe('never');
});

test('a reading series is one kind, oldest first, and draws as a polyline', () => {
  const readings: Reading[] = [
    { kind: 'heart_rate', value: 80, unit: 'bpm', recorded_at: '2026-11-21T10:02:00Z' },
    { kind: 'steps', value: 5, unit: '', recorded_at: '2026-11-21T10:01:00Z' },
    { kind: 'heart_rate', value: 70, unit: 'bpm', recorded_at: '2026-11-21T10:00:00Z' },
  ];
  const pts = series(readings, 'heart_rate');
  expect(pts.map((p) => p.v)).toEqual([70, 80]);
  const path = polyline(pts, 100, 50, 0);
  expect(path).toBe('0.0,50.0 100.0,0.0');
  expect(polyline([], 100, 50)).toBe('');
  expect(polyline([pts[0]], 100, 50)).toBe('50.0,25.0');
});

test('check-in counts and day buckets', () => {
  const mk = (status: string, at: string): Checkin => ({
    id: at, scheduled_at: at, asked_at: null, status, question_kind: 'mood', question_text: '', answered_at: null,
    answer_summary: '', answer_text: '', answered_via: 'none',
  });
  const list = [mk('answered', '2026-11-21T10:00:00Z'), mk('missed', '2026-11-21T11:00:00Z'), mk('skipped_quiet', '2026-11-20T23:00:00Z'), mk('scheduled', '2026-11-22T08:00:00Z')];
  expect(checkinCounts(list)).toEqual({ answered: 1, missed: 1, skippedQuiet: 1, other: 1 });
  const days = byDay(list, 'scheduled_at');
  expect(days.map((d) => d.day)).toEqual(['2026-11-22', '2026-11-21', '2026-11-20']);
  expect(days[1].items).toHaveLength(2);
});

import type { Visit } from '@/lib/types';
import { timeline, visitLine } from '@/lib/view';

const visit: Visit = {
  id: 'v1', person_id: 'p1', carer_member_id: null, carer_name: 'Sravani', planned_start: '2026-11-23T10:00:00+00:00',
  planned_end: '2026-11-23T11:00:00+00:00', purpose: 'Morning visit', status: 'planned', arrived_at: null, departed_at: null,
  outcome: '', notes: '', handover: '', created_at: '2026-11-22T09:00:00+00:00', updated_at: '2026-11-22T09:00:00+00:00',
  tasks: [{ id: 't1', visit_id: 'v1', title: 'Breakfast', position: 0, done: true, done_at: null, note: '' },
    { id: 't2', visit_id: 'v1', title: 'Medication', position: 1, done: false, done_at: null, note: '' }],
};
const checkin: Checkin = {
  id: 'c1', scheduled_at: '2026-11-23T09:30:00+00:00', asked_at: '2026-11-23T09:30:00+00:00', status: 'answered', question_kind: 'wellbeing',
  question_text: 'How are you?', answered_at: '2026-11-23T09:31:00+00:00', answer_summary: 'Said they were well.', answer_text: '', answered_via: 'voice',
};

test('the timeline names the source of every line and sorts newest first', () => {
  const missed = { ...visit, id: 'v2', status: 'missed' as const, planned_start: '2026-11-23T14:00:00+00:00', planned_end: '2026-11-23T15:00:00+00:00' };
  const closed = { ...base, id: 'a1', kind: 'help_requested', severity: 'sos' as const, status: 'resolved' as const, resolved_at: '2026-11-23T12:00:00+00:00', outcome: 'all_well' as const };
  const open = { ...base, id: 'a2', status: 'open' as const };
  const rows = timeline([checkin], [visit, missed], [closed, open]);
  expect(rows.map((r) => r.id)).toEqual(['v-v2', 'a-a1', 'v-v1', 'c-c1']);
  expect(rows.map((r) => r.source)).toEqual(['server', 'watch', 'carer', 'watch']);
  expect(rows.find((r) => r.id === 'v-v2')?.tone).toBe('urgent');
  expect(rows.some((r) => r.id === 'a-a2')).toBe(false);
});

test('a visit line says who, when, what and how many tasks were done', () => {
  expect(visitLine(visit)).toBe('Sravani, 10:00 to 11:00: Morning visit; 1 of 2 tasks done.');
  expect(visitLine({ ...visit, status: 'departed', outcome: 'all_well', handover: 'Ate well.' })).toBe('Sravani, 10:00 to 11:00. Outcome: All well; 1 of 2 tasks done. Handover: Ate well.');
  expect(visitLine({ ...visit, carer_name: '', tasks: [], purpose: '' })).toBe('A carer, 10:00 to 11:00.');
});

import { freshness, headline } from '@/lib/view';

test('a reading says when it was recorded and received, and is called stale or unavailable plainly', () => {
  const now = new Date('2026-11-24T10:00:00Z');
  expect(freshness(undefined, 30, now)).toEqual({ state: 'unavailable', words: 'No reading yet' });
  const fresh = freshness({ recorded_at: '2026-11-24T09:58:00Z', received_at: '2026-11-24T09:59:00Z' }, 30, now);
  expect(fresh.state).toBe('fresh'); expect(fresh.words).toContain('Recorded 2 min ago'); expect(fresh.words).toContain('received 1 min ago');
  const late = freshness({ recorded_at: '2026-11-24T09:00:00Z', received_at: '2026-11-24T09:45:00Z' }, 30, now);
  expect(late.state).toBe('stale'); expect(late.words).toContain('45 min after it was recorded');
});

test('the headline reads the alerts first, then the watch, the last check-in and the next visit', () => {
  const now = new Date('2026-11-24T10:00:00Z');
  const base = { open_alerts: [], devices: [{ last_sync_at: '2026-11-24T09:55:00Z', on_wrist: true }], last_checkin: { status: 'answered', scheduled_at: '2026-11-24T09:30:00Z' } };
  expect(headline({ ...base, open_alerts: [{ severity: 'sos', status: 'open' }] }, null, now)).toBe('1 alert waiting for someone');
  expect(headline({ ...base, open_alerts: [{ severity: 'sos', status: 'acknowledged' }] }, null, now)).toBe('Someone is on their way');
  expect(headline(base, { carer_name: 'Sravani', planned_start: '2026-11-24T11:00:00+00:00', status: 'planned' }, now)).toBe('All well: last check-in answered 30 min ago, Sravani due at 11:00');
  expect(headline({ ...base, devices: [{ last_sync_at: '2026-11-24T07:00:00Z', on_wrist: true }] }, null, now)).toContain('watch last synced 3 h ago');
  expect(headline({ ...base, devices: [] }, null, now)).toContain('no watch paired');
});

test('the headline never says All well when the system cannot vouch for it', () => {
  const now = new Date('2026-11-24T10:00:00Z');
  const base = { open_alerts: [], devices: [{ last_sync_at: '2026-11-24T09:55:00Z', on_wrist: true }], last_checkin: { status: 'answered', scheduled_at: '2026-11-24T09:30:00Z' } };
  expect(headline(base, null, now, {})).toMatch(/^All well/);
  for (const [flag, words] of [['dataStale', 'could not be refreshed'], ['monitoringPaused', 'monitoring is paused'], ['deliveryProblem', 'not getting out']] as const) {
    const h = headline(base, null, now, { [flag]: true });
    expect(h).not.toMatch(/^All well/);
    expect(h).toMatch(/^Cannot confirm all is well/);
    expect(h).toContain(words);
  }
  // A problem the person already has is still said first, with the system problem after it.
  const h = headline({ ...base, devices: [] }, null, now, { deliveryProblem: true });
  expect(h).toContain('no watch paired'); expect(h).toContain('not getting out');
});
