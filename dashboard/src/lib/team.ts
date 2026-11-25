import type { PlannedVisit, SearchHit } from './types';

/** Where a search result leads: always to the person's own record, where the server applies
 * every rule again. Nothing is shown here that the record would not show. */
export function hitHref(hit: SearchHit): string {
  const tab = hit.kind === 'visit' ? 'care' : hit.kind === 'alert' ? 'alerts' : 'overview';
  return `/people/${encodeURIComponent(hit.person_id)}?tab=${tab}`;
}

export const KIND_WORDS: Record<SearchHit['kind'], string> = { person: 'Person', visit: 'Visit', alert: 'Alert' };

/** A query worth sending: at least two characters once spaces are collapsed. */
export function searchable(q: string): boolean {
  return q.split(/\s+/).join(' ').trim().length >= 2;
}

/** Visits grouped by calendar day (UTC), in time order, for the planning view. */
export function byDayPlan(visits: PlannedVisit[]): { day: string; visits: PlannedVisit[] }[] {
  const groups = new Map<string, PlannedVisit[]>();
  for (const v of [...visits].sort((a, b) => a.planned_start.localeCompare(b.planned_start))) {
    const day = v.planned_start.slice(0, 10);
    groups.set(day, [...(groups.get(day) ?? []), v]);
  }
  return [...groups.entries()].map(([day, vs]) => ({ day, visits: vs }));
}

/** Which visits a coordinator should look at first: late or missed, then unassigned. */
export function needsAttention(v: PlannedVisit): string | null {
  if (v.status === 'missed') return 'Missed';
  if (v.status === 'late') return 'Late';
  if (v.status === 'planned' && !v.carer_member_id && !v.carer_name) return 'No carer assigned';
  return null;
}
