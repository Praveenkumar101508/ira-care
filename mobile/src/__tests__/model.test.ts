import type { Alert, Device } from '../api/types';
import { alertTitle, awaitingArrival, needsSomeone, showCall999, sortAlerts, whereabouts } from '../model/alerts';
import { deviceSummary, readingLabel, telLink, timeAgo } from '../model/format';

const base: Alert = {
  id: 'a', person_id: 'p', kind: 'low_battery', severity: 'notice', status: 'open',
  opened_at: '2026-11-21T10:00:00Z', current_tier: 0, acknowledged_at: null, resolved_at: null, details: {},
};

test('alerts are sorted most serious and unacknowledged first', () => {
  const notice = { ...base, id: 'n' };
  const sos = { ...base, id: 's', kind: 'help_requested', severity: 'sos' as const, opened_at: '2026-11-21T10:05:00Z' };
  const urgent = { ...base, id: 'u', kind: 'missed_checkins', severity: 'urgent' as const };
  const acked = { ...base, id: 'k', kind: 'no_answer_no_movement', severity: 'sos' as const, status: 'acknowledged' as const };
  expect(sortAlerts([notice, acked, urgent, sos]).map((a) => a.id)).toEqual(['s', 'u', 'n', 'k']);
});

test('alert words describe what was observed', () => {
  expect(alertTitle({ ...base, kind: 'no_answer_no_movement' })).toBe('No answer and no movement');
  expect(alertTitle({ ...base, kind: 'something_new' })).toBe('something new');
  expect(alertTitle({ ...base, kind: 'suspected_fall' })).toBe('Suspected fall');
  for (const kind of ['help_requested', 'heart_rate_outside_range', 'missed_checkins', 'suspected_fall']) {
    expect(alertTitle({ ...base, kind }).toLowerCase()).not.toMatch(/diagnos|cardiac|stroke/);
  }
});

test('a notice never needs someone to go, an open SOS does, an acknowledged one no longer', () => {
  expect(needsSomeone(base)).toBe(false);
  expect(needsSomeone({ ...base, severity: 'sos' })).toBe(true);
  expect(needsSomeone({ ...base, severity: 'sos', status: 'acknowledged' })).toBe(false);
});

test('the call 999 screen follows the backend flag and stops when resolved', () => {
  expect(showCall999(base)).toBe(false);
  expect(showCall999({ ...base, details: { show_call_999_screen: true } })).toBe(true);
  expect(showCall999({ ...base, status: 'resolved', details: { show_call_999_screen: true } })).toBe(false);
});

test('time ago in plain words', () => {
  const now = new Date('2026-11-21T12:00:00Z');
  expect(timeAgo('2026-11-21T11:59:40Z', now)).toBe('just now');
  expect(timeAgo('2026-11-21T11:40:00Z', now)).toBe('20 min ago');
  expect(timeAgo('2026-11-21T09:00:00Z', now)).toBe('3 h ago');
  expect(timeAgo('2026-11-18T09:00:00Z', now)).toBe('3 days ago');
  expect(timeAgo(null, now)).toBe('never');
  expect(timeAgo('garbage', now)).toBe('unknown');
});

test('readings and the watch are described, never judged', () => {
  expect(readingLabel({ kind: 'heart_rate', value: 71.6, unit: 'bpm', recorded_at: '' })).toBe('Heart rate 72 bpm');
  expect(readingLabel({ kind: 'on_wrist', value: 0, unit: '', recorded_at: '' })).toBe('Watch off the wrist');
  const device: Device = { id: 'd', platform: 'wearos', status: 'paired', battery_percent: 12, on_wrist: false,
    background_sensor_permission: false, last_sync_at: '2026-11-21T11:00:00Z', last_seen_at: null };
  expect(deviceSummary(device, new Date('2026-11-21T12:00:00Z'))).toBe('off the wrist, battery 12 %, sensor permission off, synced 1 h ago');
  expect(deviceSummary(undefined)).toBe('No watch paired');
});

test('telephone links keep digits and plus only', () => {
  expect(telLink('+44 0000 000001')).toBe('tel:+440000000001');
  expect(telLink('')).toBe('');
});

test('whereabouts: at home, away, unknown, and unavailable are all said plainly', () => {
  const person = { id: 'p', display_name: 'T', preferred_name: '', timezone: 'UTC', status: 'active' as const, home_address: '1 Example Street' };
  const sos = { ...base, severity: 'sos' as const, kind: 'help_requested' };
  expect(whereabouts(sos, person)).toEqual({ line: 'Location unavailable from the watch. Home address: 1 Example Street', mapLink: null });
  const home = whereabouts({ ...sos, location_latitude: 51.5002, location_longitude: -0.1201, location_accuracy_m: 25, at_home: true }, person);
  expect(home.line).toBe('At home: 1 Example Street (±25 m)');
  expect(home.mapLink).toContain('openstreetmap.org/?mlat=51.50020&mlon=-0.12010');
  const away = whereabouts({ ...sos, location_latitude: 51.518, location_longitude: -0.12, at_home: false }, person);
  expect(away.line).toMatch(/^Away from home\. 51\.51800, -0\.12000\. Home: 1 Example Street$/);
  const unknown = whereabouts({ ...sos, location_latitude: 51.5, location_longitude: -0.12, at_home: null }, undefined);
  expect(unknown.line).toContain('whether this is home is unknown');
});

test('an acknowledged alert awaits arrival until a responder says they have arrived', () => {
  const acked = { ...base, severity: 'sos' as const, status: 'acknowledged' as const };
  expect(awaitingArrival(acked)).toBe(true);
  expect(awaitingArrival({ ...acked, acknowledgements: [{ id: 'k', kind: 'on_my_way', note: '', acknowledged_at: '2026-11-21T10:01:00Z' }] })).toBe(true);
  expect(awaitingArrival({ ...acked, acknowledgements: [{ id: 'k', kind: 'arrived', note: '', acknowledged_at: '2026-11-21T10:09:00Z' }] })).toBe(false);
  expect(awaitingArrival({ ...acked, status: 'resolved' })).toBe(false);
});
