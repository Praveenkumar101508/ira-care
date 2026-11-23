import type { Reminder } from '../api/types';
import { REMINDER_WORDS, reminderActions, reminderTitle } from '../model/reminders';

const base: Reminder = {
  id: 'r1', person_id: 'p', kind: 'reminder', text: 'ring Sravani', medication_id: null, due_at: '2026-11-23T15:00:00+00:00',
  status: 'pending', snoozed_until: null, source: 'conversation', asked_count: 0, done_at: null,
};

test('a reminder reads as its time and what it is for', () => {
  expect(reminderTitle(base)).toBe('15:00 · ring Sravani');
  expect(reminderTitle({ ...base, kind: 'hydration' })).toBe('15:00 · A drink of water');
  expect(reminderTitle({ ...base, kind: 'medication', text: 'Metformin' })).toBe('15:00 · Medication: Metformin');
});

test('open reminders can be finished, put off or skipped; finished ones cannot', () => {
  expect(reminderActions(base)).toEqual(['confirm', 'snooze', 'skip']);
  expect(reminderActions({ ...base, status: 'confirming' })).toEqual(['confirm', 'snooze', 'skip']);
  for (const status of ['confirmed', 'skipped', 'missed', 'cancelled'] as const) expect(reminderActions({ ...base, status })).toEqual([]);
});

test('reminder words describe, never judge', () => {
  for (const w of Object.values(REMINDER_WORDS)) expect(w.toLowerCase()).not.toMatch(/fail|forgot|non-?compliant|refus/);
});
