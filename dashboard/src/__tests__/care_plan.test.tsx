import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { CarePlanSection } from '@/components/CarePlan';
import { draftFrom, draftProblems, may, reviewWords } from '@/lib/carePlan';
import type { CarePlan, Permissions } from '@/lib/types';

const v = (version: number, extra: Partial<CarePlan> = {}): CarePlan => ({
  id: `v${version}`, person_id: 'p1', version, goals: `Goals v${version}`, support: 'Help with breakfast',
  routine: [{ what: 'Breakfast and tablets', when: '08:00', who: 'carer' }], risks: 'Falls on the stairs',
  emergency: 'Ring her daughter', review_on: '2027-02-01', reason: `Reason ${version}`, author_user_id: null,
  author_name: 'Manager', created_at: `2026-11-2${version}T10:00:00Z`, ...extra,
});
const perms = (actions: Permissions['actions']): Permissions => ({
  role: 'member', label: 'Carer', who: '', sees_everyone: false, can_read: true, can_act: true, can_manage: false, actions,
});

test('nothing that changes the record is allowed without an answer from the server', () => {
  expect(may(null, 'edit_care_plan')).toBe(false);
  expect(may(perms({}), 'edit_care_plan')).toBe(false);
  expect(may(perms({ edit_care_plan: false }), 'edit_care_plan')).toBe(false);
  expect(may(perms({ edit_care_plan: true }), 'edit_care_plan')).toBe(true);
});

test('a draft starts from the current version and must say why it changes', () => {
  const d = draftFrom(v(3));
  expect(d.based_on).toBe(3); expect(d.reason).toBe(''); expect(d.routine).toEqual(v(3).routine);
  expect(draftFrom(null).based_on).toBe(0);
  expect(draftProblems(d)).toContain('Say why the plan is changing.');
  expect(draftProblems({ ...d, reason: 'New visit time' })).toEqual([]);
  expect(draftProblems({ ...draftFrom(null), reason: 'x' })).toContain('The plan is empty: write at least one section.');
  expect(draftProblems({ ...d, reason: 'x', routine: [{ what: ' ', when: '', who: 'carer' }] })).toContain('Every routine line needs to say what is done.');
});

test('a passed review date is called out', () => {
  const today = new Date('2027-03-01T00:00:00Z');
  expect(reviewWords(v(1), today)).toEqual({ words: 'Review was due on 2027-02-01', overdue: true });
  expect(reviewWords(v(1, { review_on: '2027-06-01' }), today).overdue).toBe(false);
  expect(reviewWords(v(1, { review_on: null }), today).words).toBe('No review date set');
});

test('someone who may not edit reads the plan and sees no way to change it', () => {
  render(<CarePlanSection plan={v(2)} history={[v(2), v(1)]} canEdit={false} busy={false} onSave={vi.fn()} />);
  expect(screen.getByTestId('plan-v2')).toHaveTextContent('Goals v2');
  expect(screen.queryByText('Change the plan')).toBeNull();
  expect(screen.getByText('Only a care manager can change the plan.')).toBeInTheDocument();
  // An earlier version can be opened, and is labelled as no longer current.
  fireEvent.click(screen.getByText('Show'));
  expect(screen.getByTestId('plan-v1')).toHaveTextContent('Version 1 (no longer current)');
});

test('a care manager saves a new version based on the one shown, with a reason', async () => {
  const onSave = vi.fn(async () => {});
  render(<CarePlanSection plan={v(2)} history={[v(2), v(1)]} canEdit busy={false} onSave={onSave} />);
  fireEvent.click(screen.getByText('Change the plan'));
  const save = screen.getByText('Save as a new version');
  expect(save).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Why the plan is changing'), { target: { value: 'Evening visit added' } });
  fireEvent.click(screen.getByText('Add a routine line'));
  fireEvent.change(screen.getByLabelText('Routine 2: what'), { target: { value: 'Evening call' } });
  fireEvent.change(screen.getByLabelText('Routine 2: who'), { target: { value: 'family' } });
  expect(save).not.toBeDisabled();
  fireEvent.click(save);
  await vi.waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  const sent = (onSave.mock.calls[0] as unknown[])[0] as ReturnType<typeof draftFrom>;
  expect(sent.based_on).toBe(2); expect(sent.reason).toBe('Evening visit added');
  expect(sent.routine[1]).toEqual({ what: 'Evening call', when: '', who: 'family' });
});

test('a failed save keeps what was typed', async () => {
  const onSave = vi.fn(async () => { throw new Error('409'); });
  render(<CarePlanSection plan={v(2)} history={[v(2)]} canEdit busy={false} onSave={onSave} />);
  fireEvent.click(screen.getByText('Change the plan'));
  fireEvent.change(screen.getByLabelText('Why the plan is changing'), { target: { value: 'Mine' } });
  fireEvent.click(screen.getByText('Save as a new version'));
  await vi.waitFor(() => expect(onSave).toHaveBeenCalled());
  expect(screen.getByTestId('plan-form')).toBeInTheDocument();
  expect(screen.getByLabelText('Why the plan is changing')).toHaveValue('Mine');
});
