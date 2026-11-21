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
