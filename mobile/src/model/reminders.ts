import type { Reminder } from '../api/types';

/** Plain words for a reminder's state. */
export const REMINDER_WORDS: Record<Reminder['status'], string> = {
  pending: 'Waiting', asked: 'Asked on the watch', confirming: 'Checking once more', confirmed: 'Done',
  snoozed: 'Asked again later', skipped: 'Not today', missed: 'No answer', cancelled: 'Cancelled',
};

export function reminderTitle(r: Reminder): string {
  const when = r.due_at.slice(11, 16);
  if (r.kind === 'hydration') return `${when} · A drink of water`;
  if (r.kind === 'medication') return `${when} · Medication: ${r.text}`;
  return `${when} · ${r.text}`;
}

/** What family may do with a reminder from the phone: finish it, put it off, or skip it, while it is open. */
export function reminderActions(r: Reminder): ('confirm' | 'snooze' | 'skip')[] {
  return ['pending', 'asked', 'confirming', 'snoozed'].includes(r.status) ? ['confirm', 'snooze', 'skip'] : [];
}
