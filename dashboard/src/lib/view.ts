import type { Alert, Checkin, Reading } from './types';

export const ALERT_WORDS: Record<string, string> = {
  help_requested: 'Asked for help',
  no_answer_no_movement: 'No answer and no movement',
  heart_rate_outside_range: 'Heart rate outside the set range',
  off_wrist_during_check: 'No answer and watch off the wrist',
  missed_checkins: 'Several check-ins missed in a row',
  off_wrist: 'Watch off the wrist',
  low_battery: 'Watch battery low',
  sensor_permission_off: 'Sensor permission switched off',
  no_sync: 'Watch has not synced',
  manual: 'Raised by a carer',
};

export const alertWords = (a: Alert) => ALERT_WORDS[a.kind] ?? a.kind.replace(/_/g, ' ');

const RANK: Record<Alert['severity'], number> = { sos: 0, urgent: 1, notice: 2 };

export function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort((a, b) =>
    (Number(a.status === 'acknowledged') - Number(b.status === 'acknowledged')) ||
    (RANK[a.severity] - RANK[b.severity]) || a.opened_at.localeCompare(b.opened_at));
}

/** One row per person for the overview: the worst open alert, if any. */
export function worstOpenAlert(alerts: Alert[], personId: string): Alert | undefined {
  return sortAlerts(alerts.filter((a) => a.person_id === personId && (a.status === 'open' || a.status === 'escalating')))[0];
}

export function timeAgo(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return 'never';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return 'unknown';
  const s = Math.max(0, Math.round((now.getTime() - t) / 1000));
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

/** Points for a simple time chart: oldest first, numbers only. */
export function series(readings: Reading[], kind: string): { t: number; v: number }[] {
  return readings.filter((r) => r.kind === kind).map((r) => ({ t: new Date(r.recorded_at).getTime(), v: r.value }))
    .filter((p) => !Number.isNaN(p.t)).sort((a, b) => a.t - b.t);
}

/** An SVG polyline path for `points` inside a width x height box. */
export function polyline(points: { t: number; v: number }[], width: number, height: number, pad = 4): string {
  if (points.length === 0) return '';
  const ts = points.map((p) => p.t), vs = points.map((p) => p.v);
  const t0 = Math.min(...ts), t1 = Math.max(...ts), v0 = Math.min(...vs), v1 = Math.max(...vs);
  const x = (t: number) => (t1 === t0 ? width / 2 : pad + ((t - t0) / (t1 - t0)) * (width - 2 * pad));
  const y = (v: number) => (v1 === v0 ? height / 2 : height - pad - ((v - v0) / (v1 - v0)) * (height - 2 * pad));
  return points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
}

export interface CheckinCounts { answered: number; missed: number; skippedQuiet: number; other: number }

export function checkinCounts(checkins: Checkin[]): CheckinCounts {
  const c: CheckinCounts = { answered: 0, missed: 0, skippedQuiet: 0, other: 0 };
  for (const x of checkins) {
    if (x.status === 'answered') c.answered += 1;
    else if (x.status === 'missed' || x.status === 'escalated') c.missed += 1;
    else if (x.status === 'skipped_quiet') c.skippedQuiet += 1;
    else c.other += 1;
  }
  return c;
}

/** Per-day buckets, newest first, for the timeline. */
export function byDay<T>(items: T[], key: keyof T): { day: string; items: T[] }[] {
  const map = new Map<string, T[]>();
  for (const item of items) {
    const iso = String((item as Record<string, unknown>)[key as string] ?? '');
    const day = iso.slice(0, 10) || 'unknown';
    map.set(day, [...(map.get(day) ?? []), item]);
  }
  return [...map.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([day, list]) => ({ day, items: list }));
}
