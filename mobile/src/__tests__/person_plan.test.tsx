import React from 'react';
import renderer, { act } from 'react-test-renderer';
import type { CareClient } from '../api/client';
import type { Person } from '../api/types';
import { ThemeProvider } from '../appearance/ThemeContext';
import { DEFAULT_APPEARANCE } from '../appearance/theme';
import { PersonScreen, medicationLine } from '../screens/PersonScreen';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null };
const all = (n: unknown, out: Node[] = []): Node[] => {
  if (Array.isArray(n)) { n.forEach((x) => all(x, out)); return out; }
  if (n && typeof n === 'object') { out.push(n as Node); ((n as Node).children ?? []).forEach((c) => all(c, out)); }
  return out;
};
const text = (tree: unknown) => all(tree).flatMap((x) => (x.children ?? []).filter((c) => typeof c === 'string') as string[]).join(' ');
const tick = () => act(async () => { await new Promise((res) => setTimeout(res, 20)); });
const press = (r: renderer.ReactTestRenderer, id: string) => {
  const n = r.root.findAll((x) => x.props.testID === id && typeof x.props.onPress === 'function')[0];
  if (!n) throw new Error(`nothing to press: ${id}`);
  act(() => { (n.props.onPress as () => void)(); });
};

const person: Person = { id: 'p1', display_name: 'Ada Hartley', preferred_name: 'Ada', timezone: 'Europe/London', status: 'active' };
const meds = [
  { id: 'm1', person_id: 'p1', name: 'the morning tablets', dose: 'one', times: ['08:00:00'], notes: '', active: true },
  { id: 'm2', person_id: 'p1', name: 'the evening tablet', dose: 'one', times: ['20:00:00'], notes: '', active: true },
  { id: 'm3', person_id: 'p1', name: 'an old prescription', dose: 'two', times: ['12:00:00'], notes: '', active: false },
];
const plan = { id: 'pl1', person_id: 'p1', version: 1, goals: 'Stay at home safely.', support: 'A morning visit.', routine: [{ what: 'breakfast', when: '08:00', who: 'carer' }],
  risks: 'Falls on the stairs.', emergency: 'Call the daughter first.', review_on: '2027-01-01', reason: 'first plan' };

const fakeClient = (withPlan: boolean) => ({
  status: async () => ({ person, devices: [], latest_readings: [], open_alerts: [], last_checkin: null }),
  checkins: async () => ({ checkins: [], answers_included: false }),
  circle: async () => [], consents: async () => ({ consents: [], effective: { transcripts_family: false, transcripts_carer: false }, retention_months: 12 }),
  reports: async () => [], visits: async () => [], profile: async () => null, reminders: async () => [], memory: async () => [],
  medications: async () => meds, plan: async () => (withPlan ? plan : null),
}) as unknown as CareClient;

const mount = async (withPlan: boolean) => {
  let r!: renderer.ReactTestRenderer;
  await act(async () => {
    r = renderer.create(<ThemeProvider appearance={DEFAULT_APPEARANCE}><PersonScreen client={fakeClient(withPlan)} person={person} canManage={false} onBack={() => {}} /></ThemeProvider>);
  });
  await tick();
  press(r, 'tab-plan');
  await tick();
  return r;
};

test('the Plan tab shows the active medication and the current care plan', async () => {
  const r = await mount(true);
  const shown = text(r.toJSON());
  expect(shown).toContain('the morning tablets · one · 08:00');
  expect(shown).toContain('the evening tablet · one · 20:00');
  expect(shown).not.toContain('an old prescription');
  expect(shown).toContain('Goals: Stay at home safely.');
  expect(shown).toContain('08:00 · breakfast (carer)');
  expect(shown).toContain('Risks: Falls on the stairs.');
  expect(shown).toContain('review on 2027-01-01');
  await act(async () => { r.unmount(); });
});

test('without a care plan the Plan tab says so instead of showing an empty card', async () => {
  const r = await mount(false);
  const shown = text(r.toJSON());
  expect(shown).toContain('No care plan written yet.');
  expect(shown).not.toContain('Goals:');
  await act(async () => { r.unmount(); });
});

test('a medication line is the name, the dose and the times of day', () => {
  expect(medicationLine(meds[0])).toBe('the morning tablets · one · 08:00');
  expect(medicationLine({ ...meds[0], dose: '', times: [] })).toBe('the morning tablets');
  expect(medicationLine({ ...meds[0], times: ['09:00:00', '21:00:00'] })).toBe('the morning tablets · one · 09:00, 21:00');
});
