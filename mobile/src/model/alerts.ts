import type { Alert, Person } from '../api/types';

/** Plain words for an alert kind: what was observed, never a diagnosis. */
export const ALERT_WORDS: Record<string, string> = {
  help_requested: 'Asked for help',
  suspected_fall: 'Suspected fall',
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

export function alertTitle(alert: Alert): string {
  return ALERT_WORDS[alert.kind] ?? alert.kind.replace(/_/g, ' ');
}

const SEVERITY_RANK: Record<Alert['severity'], number> = { sos: 0, urgent: 1, notice: 2 };

/** Most serious first, then the oldest unacknowledged, then the rest by time. */
export function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort((a, b) => {
    const ack = Number(a.status === 'acknowledged') - Number(b.status === 'acknowledged');
    if (ack !== 0) return ack;
    const sev = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    if (sev !== 0) return sev;
    return a.opened_at.localeCompare(b.opened_at);
  });
}

/** Someone said "I'm on my way" and nobody has arrived yet. */
export function awaitingArrival(alert: Alert): boolean {
  return alert.status === 'acknowledged' && !(alert.acknowledgements ?? []).some((a) => a.kind === 'arrived' || a.kind === 'checked_in_person');
}

export function needsSomeone(alert: Alert): boolean {
  return (alert.status === 'open' || alert.status === 'escalating') && alert.severity !== 'notice';
}

/** The family app's tier-3 instruction, set by the backend when no monitoring centre exists. */
export function showCall999(alert: Alert): boolean {
  return alert.details?.show_call_999_screen === true && alert.status !== 'resolved';
}

/** Where the person is, for whoever is going: at home, away, or honestly unknown. */
export function whereabouts(alert: Alert, person?: Person): { line: string; mapLink: string | null } {
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
