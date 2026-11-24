import type { Alert, Person, Reminder, Visit } from '../api/types';

/** The three ways the one app is used. The server decides from the care circle (`/me/experience`). */
export type Experience = 'wearer' | 'family' | 'carer';

export interface Membership { id: string; person_id: string; person_name: string; role: string; relationship: string; display_name: string; tier: number }

/** The home cards each experience has, in their default order. The person reorders them in Settings. */
export const HOME_CARDS: Record<Experience, { key: string; title: string }[]> = {
  wearer: [
    { key: 'help', title: 'Help' }, { key: 'next_visit', title: 'Next visit' }, { key: 'reminders', title: 'Reminders' },
    { key: 'talk', title: 'Talk to IRA' }, { key: 'sharing', title: 'Who sees what' },
  ],
  family: [
    { key: 'alerts', title: 'Alerts' }, { key: 'person', title: 'The person' }, { key: 'updates', title: 'Updates' },
    { key: 'watch', title: 'The watch' }, { key: 'next_visit', title: 'Next visit' },
  ],
  carer: [
    { key: 'alerts', title: 'Alerts' }, { key: 'visits', title: "Today's visits" }, { key: 'people', title: 'People' },
    { key: 'handover', title: 'Handover' },
  ],
};

export const TABS = ['home', 'care', 'alerts', 'settings'] as const;
export type Tab = (typeof TABS)[number];
export const TAB_WORDS: Record<Tab, string> = { home: 'Home', care: 'Care', alerts: 'Alerts', settings: 'Settings' };

/** The person this account starts on: the remembered one if still there, else the first they belong to. */
export function pickPerson(people: Person[], memberships: Membership[], remembered: string | null | undefined): Person | undefined {
  const mine = new Set(memberships.map((m) => m.person_id));
  const candidates = people.filter((p) => mine.size === 0 || mine.has(p.id));
  return candidates.find((p) => p.id === remembered) ?? candidates[0] ?? people[0];
}

/** The next visit for a person: the one in progress, else the next planned, within the next day. */
export function nextVisit(visits: Visit[], now = new Date()): Visit | undefined {
  const here = visits.find((v) => v.status === 'arrived');
  if (here) return here;
  return visits
    .filter((v) => (v.status === 'planned' || v.status === 'late') && new Date(v.planned_end).getTime() > now.getTime()
      && new Date(v.planned_start).getTime() < now.getTime() + 36 * 3600_000)
    .sort((a, b) => a.planned_start.localeCompare(b.planned_start))[0];
}

/** What the wearer's next-visit card says. */
export function nextVisitWords(v: Visit | undefined): string {
  if (!v) return 'No visit planned in the next day.';
  const who = v.carer_name || 'Your carer';
  const t = v.planned_start.slice(11, 16);
  if (v.status === 'arrived') return `${who} is here.`;
  return `${who} is coming at ${t}.`;
}

/** Today's visits for a carer, across the people they visit, in time order. */
export function todaysVisits(visits: Visit[], now = new Date()): Visit[] {
  const day = now.toISOString().slice(0, 10);
  return visits.filter((v) => v.planned_start.slice(0, 10) === day && v.status !== 'cancelled')
    .sort((a, b) => a.planned_start.localeCompare(b.planned_start));
}

/** The reminders a wearer sees: open ones, soonest first. */
export function openReminders(reminders: Reminder[]): Reminder[] {
  return reminders.filter((r) => ['pending', 'asked', 'confirming', 'snoozed'].includes(r.status)).sort((a, b) => a.due_at.localeCompare(b.due_at));
}

/** The count on the Alerts tab: open alerts that need someone, for the people this account belongs to. */
export function alertBadge(alerts: Alert[], memberships: Membership[]): number {
  const mine = new Set(memberships.map((m) => m.person_id));
  return alerts.filter((a) => (mine.size === 0 || mine.has(a.person_id)) && (a.status === 'open' || a.status === 'escalating')).length;
}
