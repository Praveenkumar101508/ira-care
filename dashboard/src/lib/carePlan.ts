import type { CarePlan, CarePlanDraft, Permissions, PlanWho, StaffAction } from './types';

/** Whether to show an action. Without an answer from the server, nothing that changes the
 *  record is shown: hiding a button is a courtesy, the server checks every request anyway. */
export function may(perms: Permissions | null, action: StaffAction): boolean {
  return !!perms?.actions?.[action];
}

export const WHO_WORDS: Record<PlanWho, string> = { carer: 'Carer', family: 'Family', person: 'The person themselves' };

/** A draft to edit, started from the current plan (or blank for the first one). */
export function draftFrom(plan: CarePlan | null): CarePlanDraft {
  return {
    based_on: plan?.version ?? 0, goals: plan?.goals ?? '', support: plan?.support ?? '',
    routine: plan ? plan.routine.map((r) => ({ ...r })) : [], risks: plan?.risks ?? '', emergency: plan?.emergency ?? '',
    review_on: plan?.review_on ?? null, reason: '',
  };
}

/** What stops the draft being saved, in plain words; empty when it can be saved. */
export function draftProblems(d: CarePlanDraft): string[] {
  const out: string[] = [];
  if (!d.reason.trim()) out.push('Say why the plan is changing.');
  if (d.routine.some((r) => !r.what.trim())) out.push('Every routine line needs to say what is done.');
  if (d.routine.length > 40) out.push('A routine has at most 40 lines.');
  if (!d.goals.trim() && !d.support.trim() && !d.routine.length && !d.risks.trim() && !d.emergency.trim()) {
    out.push('The plan is empty: write at least one section.');
  }
  return out;
}

/** "Review due" when the review date has passed, so an out-of-date plan is noticed. */
export function reviewWords(plan: CarePlan, today: Date = new Date()): { words: string; overdue: boolean } {
  if (!plan.review_on) return { words: 'No review date set', overdue: false };
  const overdue = plan.review_on < today.toISOString().slice(0, 10);
  return { words: overdue ? `Review was due on ${plan.review_on}` : `Next review ${plan.review_on}`, overdue };
}
