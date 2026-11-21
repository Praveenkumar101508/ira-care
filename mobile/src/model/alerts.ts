import type { Alert } from '../api/types';

/** Plain words for an alert kind: what was observed, never a diagnosis. */
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

export function needsSomeone(alert: Alert): boolean {
  return (alert.status === 'open' || alert.status === 'escalating') && alert.severity !== 'notice';
}

/** The family app's tier-3 instruction, set by the backend when no monitoring centre exists. */
export function showCall999(alert: Alert): boolean {
  return alert.details?.show_call_999_screen === true && alert.status !== 'resolved';
}
