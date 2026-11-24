import type { Visit, VisitOutcome } from '../api/types';

/** Plain words for a visit's state: what is planned or what happened, never a judgement. */
export const VISIT_WORDS: Record<Visit['status'], string> = {
  planned: 'Planned', late: 'Running late', arrived: 'Carer is here', departed: 'Done',
  missed: 'Did not happen', cancelled: 'Cancelled',
};

export const VISIT_OUTCOME_WORDS: Record<VisitOutcome, string> = {
  all_well: 'All well', concern_noted: 'Concern noted', needed_help: 'Needed help',
  not_at_home: 'Not at home', declined: 'Declined the visit', other: 'Other',
};

export const VISIT_OUTCOMES: VisitOutcome[] = ['all_well', 'concern_noted', 'needed_help', 'not_at_home', 'declined', 'other'];

/** The one visit a carer acts on now: the one they are in, else the next open one. */
export function currentVisit(visits: Visit[], now = new Date()): Visit | undefined {
  const here = visits.find((v) => v.status === 'arrived');
  if (here) return here;
  return visits
    .filter((v) => (v.status === 'planned' || v.status === 'late' || v.status === 'missed') && new Date(v.planned_end).getTime() > now.getTime() - 12 * 3600_000)
    .sort((a, b) => a.planned_start.localeCompare(b.planned_start))[0];
}

export function visitTitle(v: Visit): string {
  const who = v.carer_name || 'A carer';
  return `${who} · ${v.planned_start.slice(11, 16)} to ${v.planned_end.slice(11, 16)} · ${VISIT_WORDS[v.status]}`;
}

/** What a carer may do with a visit right now. */
export function visitActions(v: Visit): ('arrive' | 'depart')[] {
  if (v.status === 'planned' || v.status === 'late' || v.status === 'missed') return ['arrive'];
  if (v.status === 'arrived') return ['depart'];
  return [];
}
