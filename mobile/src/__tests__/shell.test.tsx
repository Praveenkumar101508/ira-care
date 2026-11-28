import React from 'react';
import renderer, { act } from 'react-test-renderer';
import type { Alert, Person, Visit } from '../api/types';
import { ThemeProvider } from '../appearance/ThemeContext';
import { DEFAULT_APPEARANCE } from '../appearance/theme';
import { HomeTab, type HomeActions, type HomeData } from '../shell/HomeTab';
import { SettingsTab } from '../shell/SettingsTab';
import { TabBar } from '../shell/TabBar';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null; type?: string };
const all = (n: Node | string | null | (Node | string)[], out: Node[] = []): Node[] => {
  if (Array.isArray(n)) { n.forEach((x) => all(x, out)); return out; }
  if (n && typeof n !== 'string') { out.push(n); (n.children ?? []).forEach((c) => all(c, out)); }
  return out;
};
const byId = (tree: Node | Node[], id: string) => all(tree).find((n) => n.props.testID === id);
const text = (n: Node | undefined): string => (n ? all(n).flatMap((x) => (x.children ?? []).filter((c) => typeof c === 'string') as string[]).join(' ') : '');

const person: Person = { id: 'p1', display_name: 'Test Person', preferred_name: 'Tess', timezone: 'UTC', status: 'active' };
const visit: Visit = { id: 'v1', person_id: 'p1', carer_name: 'Sravani', planned_start: new Date(Date.now() + 3600_000).toISOString().replace('Z', '+00:00'),
  planned_end: new Date(Date.now() + 7200_000).toISOString().replace('Z', '+00:00'), purpose: 'Morning visit', status: 'planned', arrived_at: null, departed_at: null, outcome: '', notes: '', handover: '', tasks: [] };
const open: Alert = { id: 'a1', person_id: 'p1', kind: 'help_requested', severity: 'sos', status: 'open', opened_at: new Date().toISOString(), current_tier: 1, acknowledged_at: null, resolved_at: null, details: {} };
const actions = (): HomeActions & { calls: string[] } => {
  const calls: string[] = [];
  const f = (n: string) => () => { calls.push(n); };
  return { calls, onHelp: f('help'), onTalk: f('talk'), onAlerts: f('alerts'), onCare: f('care'), onSettings: f('settings'), onSwitchPerson: f('switch'), onOnMyWay: f('onmyway'), onArrive: f('arrive'), onDepart: f('depart'), onReminder: f('reminder') };
};
const data = (over: Partial<HomeData>): HomeData => ({ experience: 'wearer', memberships: [], people: [person], person, alerts: [], visits: [visit], reminders: [], cardOrder: [], error: '', ...over });
const mount = (el: React.ReactElement, appearance = DEFAULT_APPEARANCE) => renderer.create(<ThemeProvider appearance={appearance}>{el}</ThemeProvider>);
const render = (el: React.ReactElement, appearance = DEFAULT_APPEARANCE) => mount(el, appearance).toJSON() as Node | Node[];
/** The press handler of the element with this testID, from the component tree (host JSON has no onPress). */
const press = (r: renderer.ReactTestRenderer, id: string) => { const inst = r.root.findAll((n) => n.props.testID === id && typeof n.props.onPress === 'function')[0]; act(() => { (inst.props.onPress as () => void)(); }); };
const pressLabel = (r: renderer.ReactTestRenderer, label: string) => { const inst = r.root.findAll((n) => n.props.accessibilityLabel === label && typeof n.props.onPress === 'function')[0]; act(() => { (inst.props.onPress as () => void)(); }); };

test('the wearer sees Help first, in the fixed urgent colour, whatever their card order or wallpaper', () => {
  const a = actions();
  const r = mount(<HomeTab d={data({ cardOrder: ['sharing', 'talk', 'help'] })} a={a} />, { ...DEFAULT_APPEARANCE, wallpaper_kind: 'image', wallpaper_value: 'evening', mode: 'dark' });
  const tree = r.toJSON() as Node;
  const ids = all(tree).map((n) => n.props.testID).filter((x) => typeof x === 'string' && String(x).startsWith('card-'));
  expect(ids[0]).toBe('card-help');
  expect(ids.slice(1, 3)).toEqual(['card-sharing', 'card-talk']);
  const help = all(tree).find((n) => n.props.accessibilityLabel === 'Help');
  const style = Object.assign({}, ...([] as object[]).concat(help!.props.style as object[]));
  expect((style as { backgroundColor: string }).backgroundColor).toBe('#D32F2F');
  expect(byId(tree, 'scrim')).toBeTruthy();
  expect(text(byId(tree, 'card-next-visit'))).toContain('Sravani is coming at');
  pressLabel(r, 'Help');
  expect(a.calls).toEqual(['help']);
});

test('family see the alert that needs someone first, with I\'m on my way, and can switch person', () => {
  const a = actions();
  const other = { ...person, id: 'p2', preferred_name: 'Bob' };
  const r = mount(<HomeTab d={data({ experience: 'family', people: [person, other], alerts: [open] })} a={a} />);
  const tree = r.toJSON() as Node;
  expect(text(byId(tree, 'urgent-banner'))).toContain('1 alert needs someone');
  press(r, 'home-on-my-way');
  press(r, 'switch-p2');
  expect(a.calls).toEqual(['onmyway', 'switch']);
  expect(byId(tree, 'card-watch')).toBeTruthy();
});

test('while the server cannot be reached, family are not told "No open alerts."', () => {
  const offline = render(<HomeTab d={data({ experience: 'family', alerts: [], error: 'No connection. Check the network and try again.' })} a={actions()} />);
  expect(byId(offline, 'card-stale')).toBeTruthy();
  expect(text(byId(offline, 'card-stale'))).toContain('Alerts may be out of date');
  expect(byId(offline, 'card-alerts')).toBeUndefined();
  expect(text(offline as Node)).not.toContain('No open alerts.');
  // Negative controls: with the connection back the plain card returns, and an alert that needs
  // someone is still drawn first even while offline.
  const online = render(<HomeTab d={data({ experience: 'family', alerts: [] })} a={actions()} />);
  expect(text(byId(online, 'card-alerts'))).toContain('No open alerts.');
  const both = render(<HomeTab d={data({ experience: 'family', alerts: [open], error: 'No connection.' })} a={actions()} />);
  expect(byId(both, 'urgent-banner')).toBeTruthy();
  expect(byId(both, 'card-stale')).toBeUndefined();
});

test("the urgent banner's I'm on my way button has readable words, and the banner names the alert", () => {
  const r = mount(<HomeTab d={data({ experience: 'family', alerts: [open] })} a={actions()} />);
  const tree = r.toJSON() as Node;
  const flat = (s: unknown) => Object.assign({}, ...([] as object[]).concat(s as object[]).flat(Infinity as 1)) as { backgroundColor?: string; color?: string };
  const button = byId(tree, 'home-on-my-way')!;
  const label = all(button).find((n) => n.type === 'Text')!;
  expect(text(button)).toContain("I'm on my way");
  const hex = (v?: string) => { const h = (v ?? '').toLowerCase().replace('#', ''); return h.length === 3 ? h.split('').map((x) => x + x).join('') : h; };
  expect(hex(flat(label.props.style).color)).not.toBe(hex(flat(button.props.style).backgroundColor));
  expect(text(byId(tree, 'urgent-banner'))).toContain('Asked for help');
});

test("I'm on my way is not inside the banner's own pressable, so iOS VoiceOver can reach it", () => {
  // On iOS a pressable is one accessibility element: a button nested in it is hidden from VoiceOver.
  const a = actions();
  const r = mount(<HomeTab d={data({ experience: 'family', alerts: [open] })} a={a} />);
  const pressables = r.root.findAll((n) => typeof n.props.onPress === 'function' && n.props.accessibilityRole === 'button');
  const opener = pressables.find((n) => n.props.testID === 'urgent-banner-open')!;
  expect(opener).toBeTruthy();
  expect(opener.findAll((n) => n.props.testID === 'home-on-my-way')).toHaveLength(0);
  expect(r.root.findAll((n) => n.props.testID === 'urgent-banner')[0].findAll((n) => n.props.testID === 'home-on-my-way').length).toBeGreaterThan(0);
  press(r, 'urgent-banner-open');
  expect(a.calls).toEqual(['alerts']);
});

test("a carer sees today's visits with I've arrived, and the last handover", () => {
  const a = actions();
  const today = { ...visit, planned_start: new Date().toISOString().slice(0, 11) + '10:00:00+00:00', planned_end: new Date().toISOString().slice(0, 11) + '11:00:00+00:00' };
  const done = { ...visit, id: 'v0', status: 'departed' as const, departed_at: new Date().toISOString(), handover: 'Ate well.' };
  const r = mount(<HomeTab d={data({ experience: 'carer', visits: [today, done] })} a={a} />);
  const tree = r.toJSON() as Node;
  expect(text(byId(tree, 'card-visits'))).toContain('Sravani');
  press(r, 'arrive-v1');
  expect(a.calls).toEqual(['arrive']);
  expect(text(byId(tree, 'card-handover'))).toContain('Ate well.');
});

test('settings has the eight sections, each with an accessible label, and the appearance controls change the appearance', () => {
  let current = DEFAULT_APPEARANCE;
  const el = () => (
    <SettingsTab appearance={current} onAppearance={(x) => { current = x; }} experience="wearer" circle={[]} consents={null} pushRegistered={false}
      onRegisterPush={() => {}} username="sravani@example.com" server="https://care.example.org" onSignOut={() => {}} onPairWatch={() => {}} onOpenCare={() => {}} onPickPhoto={async () => null} />
  );
  const r = renderer.create(<ThemeProvider appearance={current}>{el()}</ThemeProvider>);
  const sections = all(r.toJSON() as Node).filter((n) => String(n.props.testID ?? '').startsWith('settings-'));
  expect(sections).toHaveLength(8);
  // Named by their visible title and description (WCAG 2.5.3), not by a separate label.
  for (const s of sections) expect(s.props.accessibilityLabel).toBeUndefined();
  press(r, 'settings-appearance');
  press(r, 'mode-dark');
  expect(current.mode).toBe('dark');
  act(() => { r.update(<ThemeProvider appearance={current}>{el()}</ThemeProvider>); });
  press(r, 'wallpaper-image-garden');
  expect(current.wallpaper_kind).toBe('image');
  act(() => { r.update(<ThemeProvider appearance={current}>{el()}</ThemeProvider>); });
  press(r, 'card-down-help');
  expect(current.card_order[1]).toBe('help');
});

test('the tab bar names its four tabs and counts the alerts that need someone', () => {
  const tree = render(<TabBar tab="home" onTab={() => {}} alertCount={2} />);
  const tabs = all(tree).filter((n) => n.props.accessibilityRole === 'tab');
  expect(tabs.map((t) => t.props.accessibilityLabel)).toEqual(['Home', 'Care', undefined, 'Settings']);
  // With a badge the name is the visible words (WCAG 2.5.3) and the hint says what the number means.
  expect(tabs[2].props.accessibilityHint).toBe('2 need someone');
  expect(tabs[0].props.accessibilityState).toEqual({ selected: true });
});
