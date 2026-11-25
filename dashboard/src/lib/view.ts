import { VISIT_OUTCOME_WORDS, type Alert, type Person, type Checkin, type Reading, type Visit, type VisitOutcome } from './types';

export const ALERT_WORDS: Record<string, string> = {
  suspected_fall: 'Suspected fall',
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
  visit_late: 'Planned visit is late',
  visit_missed: 'Planned visit did not happen',
  off_wrist_night: 'Watch off the wrist overnight',
  concerning_phrase: 'Said something that needs a check',
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

/** Where the person is, for whoever is going: at home, away, or honestly unknown. */
export function whereabouts(alert: Alert, person?: Pick<Person, 'home_address'> | null): { line: string; mapLink: string | null } {
  const address = person?.home_address?.trim() ?? '';
  const lat = alert.location_latitude, lon = alert.location_longitude;
  if (lat == null || lon == null) {
    return { line: address ? `Location unavailable from the watch. Home address: ${address}` : 'Location unavailable from the watch.', mapLink: null };
  }
  const mapLink = `https://www.openstreetmap.org/?mlat=${lat.toFixed(5)}&mlon=${lon.toFixed(5)}#map=17/${lat.toFixed(5)}/${lon.toFixed(5)}`;
  const acc = alert.location_accuracy_m != null ? ` (±${Math.round(alert.location_accuracy_m)} m)` : '';
  if (alert.at_home === true) return { line: `At home${address ? `: ${address}` : ''}${acc}`, mapLink };
  if (alert.at_home === false) return { line: `Away from home${acc}. ${lat.toFixed(5)}, ${lon.toFixed(5)}${address ? `. Home: ${address}` : ''}`, mapLink };
  return { line: `At ${lat.toFixed(5)}, ${lon.toFixed(5)}${acc}; whether this is home is unknown${address ? `. Home: ${address}` : ''}`, mapLink };
}

/** One line of the timeline, with where it came from, so a reader can tell the
 *  watch's word from a carer's note from IRA's own question. */
export type TimelineSource = 'watch' | 'carer' | 'IRA' | 'server';
export interface TimelineEntry {
  id: string; at: string; source: TimelineSource; kind: 'checkin' | 'visit' | 'alert'; title: string; detail: string;
  tone: 'ok' | 'urgent' | 'grey' | 'notice';
}

export const VISIT_WORDS: Record<Visit['status'], string> = {
  planned: 'Planned visit', late: 'Visit running late', arrived: 'Carer is here', departed: 'Visit done',
  missed: 'Visit did not happen', cancelled: 'Visit cancelled',
};

export function visitLine(v: Visit): string {
  const who = v.carer_name || 'A carer';
  const window = `${v.planned_start.slice(11, 16)} to ${v.planned_end.slice(11, 16)}`;
  const done = v.tasks.filter((t) => t.done).length;
  const tasks = v.tasks.length ? `; ${done} of ${v.tasks.length} tasks done` : '';
  if (v.status === 'departed') return `${who}, ${window}. Outcome: ${VISIT_OUTCOME_WORDS[v.outcome as Exclude<VisitOutcome, ''>] ?? v.outcome}${tasks}.${v.handover ? ` Handover: ${v.handover}` : ''}`;
  if (v.status === 'arrived') return `${who} arrived ${v.arrived_at ? v.arrived_at.slice(11, 16) : ''} (planned ${window})${tasks}.`;
  return `${who}, ${window}${v.purpose ? `: ${v.purpose}` : ''}${tasks}.`;
}

export function timeline(checkins: Checkin[], visits: Visit[], alerts: Alert[]): TimelineEntry[] {
  const out: TimelineEntry[] = [];
  for (const c of checkins) {
    out.push({
      id: `c-${c.id}`, at: c.answered_at ?? c.asked_at ?? c.scheduled_at, kind: 'checkin',
      source: c.status === 'answered' ? 'watch' : 'IRA',
      title: `${c.question_kind} check-in${c.question_source === 'model' ? ' (composed)' : ''}`,
      detail: `${c.status}${c.answer_summary ? `: ${c.answer_summary}` : ''}`,
      tone: c.status === 'answered' ? 'ok' : c.status === 'missed' || c.status === 'escalated' ? 'urgent' : 'grey',
    });
  }
  for (const v of visits) {
    out.push({
      id: `v-${v.id}`, at: v.departed_at ?? v.arrived_at ?? v.planned_start, kind: 'visit',
      source: v.status === 'late' || v.status === 'missed' ? 'server' : 'carer',
      title: VISIT_WORDS[v.status], detail: visitLine(v),
      tone: v.status === 'missed' ? 'urgent' : v.status === 'late' ? 'notice' : v.status === 'departed' || v.status === 'arrived' ? 'ok' : 'grey',
    });
  }
  for (const a of alerts) {
    if (a.status !== 'resolved') continue; // open alerts have their own cards above the timeline
    out.push({
      id: `a-${a.id}`, at: a.resolved_at ?? a.opened_at, kind: 'alert',
      source: a.kind === 'help_requested' || a.kind === 'suspected_fall' ? 'watch' : a.kind === 'manual' ? 'carer' : 'server',
      title: alertWords(a), detail: `resolved${a.outcome ? `: ${a.outcome.replace(/_/g, ' ')}` : ''}`,
      tone: a.severity === 'notice' ? 'grey' : 'notice',
    });
  }
  return out.sort((x, y) => y.at.localeCompare(x.at));
}


/** A reading's freshness, said plainly: when it was recorded, when it reached us, and whether it is stale. */
export type Freshness = { state: 'fresh' | 'stale' | 'unavailable'; words: string };
export function freshness(reading: Pick<Reading, 'recorded_at'> & { received_at?: string | null } | undefined, staleAfterMinutes: number, now = new Date()): Freshness {
  if (!reading) return { state: 'unavailable', words: 'No reading yet' };
  const recorded = new Date(reading.recorded_at).getTime();
  if (Number.isNaN(recorded)) return { state: 'unavailable', words: 'No reading yet' };
  const ageMin = (now.getTime() - recorded) / 60000;
  const received = reading.received_at ? ` · received ${timeAgo(reading.received_at, now)}` : '';
  const lag = reading.received_at ? Math.round((new Date(reading.received_at).getTime() - recorded) / 60000) : 0;
  const lagWords = lag >= 2 ? ` (${lag} min after it was recorded)` : '';
  if (ageMin > staleAfterMinutes) return { state: 'stale', words: `Stale: recorded ${timeAgo(reading.recorded_at, now)}${received}${lagWords}` };
  return { state: 'fresh', words: `Recorded ${timeAgo(reading.recorded_at, now)}${received}${lagWords}` };
}

/** Whether the watch is on the wrist, never guessed. Four states, and only one of them is reassuring:
 *  on       the watch said so, and it synced recently
 *  off      the watch said it is off
 *  unknown  the watch has never said (null or missing): NOT "on the wrist"
 *  stale    it said so, but has not synced for longer than `staleAfterMinutes`: last known only */
export type WristState = 'on' | 'off' | 'unknown' | 'stale';
export function wristState(device: { on_wrist?: boolean | null; last_sync_at?: string | null } | undefined, staleAfterMinutes = 60, now = new Date()): { state: WristState; words: string } {
  if (!device || device.on_wrist === null || device.on_wrist === undefined) return { state: 'unknown', words: 'wrist unknown' };
  const synced = device.last_sync_at ? new Date(device.last_sync_at).getTime() : NaN;
  const last = device.on_wrist ? 'on the wrist' : 'off the wrist';
  if (Number.isNaN(synced) || (now.getTime() - synced) / 60000 > staleAfterMinutes) {
    return { state: 'stale', words: `last known ${last}, ${timeAgo(device.last_sync_at ?? null, now)}` };
  }
  return device.on_wrist ? { state: 'on', words: 'on the wrist' } : { state: 'off', words: 'off the wrist' };
}

/** The headline of a person's record: one line a coordinator reads first. "All well" only when
 *  nothing is open, nothing is being asked, and the watch is recently synced and known to be worn. */
export function headline(status: { open_alerts: { severity: string; status: string }[]; devices: { last_sync_at: string | null; on_wrist: boolean | null }[]; last_checkin: { status: string; scheduled_at: string } | null }, nextVisit?: { carer_name: string; planned_start: string; status: string } | null, now = new Date()): string {
  const open = status.open_alerts.filter((a) => a.status === 'open' || a.status === 'escalating');
  if (open.length) return `${open.length} alert${open.length > 1 ? 's' : ''} waiting for someone`;
  if (status.open_alerts.some((a) => a.status === 'asking')) return 'IRA is asking whether they are OK: no answer yet';
  const acked = status.open_alerts.filter((a) => a.status === 'acknowledged');
  if (acked.length) return 'Someone is on their way';
  const concerns: string[] = [];
  const parts: string[] = [];
  const d = status.devices[0];
  if (!d) concerns.push('no watch paired');
  else if (!d.last_sync_at || (now.getTime() - new Date(d.last_sync_at).getTime()) > 60 * 60000) concerns.push(`watch last synced ${timeAgo(d.last_sync_at, now)}`);
  else if (d.on_wrist === false) concerns.push('watch off the wrist');
  else if (d.on_wrist !== true) concerns.push('not known whether the watch is worn');
  if (status.last_checkin) parts.push(`last check-in ${status.last_checkin.status} ${timeAgo(status.last_checkin.scheduled_at, now)}`);
  if (nextVisit) parts.push(nextVisit.status === 'arrived' ? `${nextVisit.carer_name || 'a carer'} is there now` : `${nextVisit.carer_name || 'a carer'} due at ${nextVisit.planned_start.slice(11, 16)}`);
  if (concerns.length) return 'Needs a look: ' + [...concerns, ...parts].join(', ');
  return parts.length ? 'All well: ' + parts.join(', ') : 'All well';
}


/** The escalation tier in words a reader understands: who in the circle is being reached. */
export function circleWords(tier: number): string {
  return tier <= 1 ? 'Called first' : tier === 2 ? 'Called second' : 'Called last';
}
export function reachWords(tier: number): string {
  return tier <= 0 ? 'nobody told yet' : tier === 1 ? 'family and carers told' : tier === 2 ? 'care team told' : 'everyone told';
}
