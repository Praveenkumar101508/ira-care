import React from 'react';
import renderer from 'react-test-renderer';
import { TalkScreen } from '../screens/TalkScreen';
import type { Person, TalkReply } from '../api/types';
import type { Heard, Listener, Speaker } from '../voice/voice';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null };
const all = (n: Node | Node[] | string | null, out: Node[] = []): Node[] => {
  if (Array.isArray(n)) { n.forEach((c) => all(c, out)); return out; }
  if (n && typeof n !== 'string') { out.push(n); (n.children ?? []).forEach((c) => all(c, out)); }
  return out;
};
const text = (n: Node): string => (n.children ?? []).map((c) => (typeof c === 'string' ? c : text(c))).join('');
const person = { id: 'p1', display_name: 'Ada Lovelace', preferred_name: 'Ada' } as unknown as Person;

function fakeVoice(heard: Heard = { text: 'what is on today' }, available = true) {
  const spoken: string[] = [];
  const events: string[] = [];
  const speaker: Speaker = { speak: (t) => { spoken.push(t); events.push(`speak:${t}`); }, stop: () => events.push('stop-speaking') };
  const listener: Listener = { available: () => available, listen: async () => { events.push('listen'); return heard; }, stop: () => events.push('stop-listening') };
  return { voice: { speaker, listener }, spoken, events };
}

function screen(onSend: (t: string, s: (x: string) => void) => Promise<TalkReply>, voice: ReturnType<typeof fakeVoice>['voice']) {
  const r = renderer.create(<TalkScreen person={person} onSend={onSend} onBack={() => {}} voice={voice} />);
  // The component instance carries the handlers (onPress, onChangeText, onSubmitEditing).
  const find = (id: string) => r.root.findAll((n) => n.props.testID === id && (typeof n.props.onPress === 'function' || typeof n.props.onChangeText === 'function'))[0];
  const lines = () => all(r.toJSON() as Node).filter((n) => /^talk-(you|ira)-/.test(String(n.props.testID ?? ''))).map(text);
  return { r, find, lines };
}

const reply = (sentences: string[]): TalkReply => ({ sentences, action: null, needsHelp: false });

test('IRA reads each sentence aloud as it arrives, in order', async () => {
  const v = fakeVoice();
  let mid: string[] = [];
  const onSend = async (_t: string, onSentence: (s: string) => void) => {
    onSentence('Good morning, Ada.');
    mid = [...v.spoken];
    onSentence('The carer comes at ten.');
    return reply(['Good morning, Ada.', 'The carer comes at ten.']);
  };
  const s = screen(onSend, v.voice);
  await renderer.act(async () => { (s.find('talk-input')!.props.onChangeText as (t: string) => void)('hello'); });
  await renderer.act(async () => { (s.find('talk-input')!.props.onSubmitEditing as () => void)(); });
  expect(mid).toEqual(['Good morning, Ada.']); // spoken before the second sentence came
  expect(v.spoken).toEqual(['Good morning, Ada.', 'The carer comes at ten.']);
});

test('with Read aloud off IRA is silent, and turning it off stops IRA mid-sentence', async () => {
  const v = fakeVoice();
  const s = screen(async (_t, onSentence) => { onSentence('Hello.'); return reply(['Hello.']); }, v.voice);
  await renderer.act(async () => { (s.find('talk-aloud')!.props.onPress as () => void)(); });
  expect(v.events).toContain('stop-speaking');
  await renderer.act(async () => { (s.find('talk-input')!.props.onChangeText as (t: string) => void)('hi'); });
  await renderer.act(async () => { (s.find('talk-input')!.props.onSubmitEditing as () => void)(); });
  expect(v.spoken).toEqual([]);
  expect(s.lines()).toEqual(['You: hi', 'IRA: Hello.']);
});

test('what the person says is sent exactly as if typed', async () => {
  const v = fakeVoice({ text: 'what is on today' });
  const sent: string[] = [];
  const s = screen(async (t, onSentence) => { sent.push(t); onSentence('A visit at ten.'); return reply(['A visit at ten.']); }, v.voice);
  await renderer.act(async () => { (s.find('talk-speak')!.props.onPress as () => void)(); });
  expect(sent).toEqual(['what is on today']);
  expect(s.lines()).toEqual(['You: what is on today', 'IRA: A visit at ten.']);
});

test('when nothing was heard the person is told, and nothing is sent', async () => {
  const v = fakeVoice({ error: 'nothing_heard' });
  const sent: string[] = [];
  const s = screen(async (t) => { sent.push(t); return reply([]); }, v.voice);
  await renderer.act(async () => { (s.find('talk-speak')!.props.onPress as () => void)(); });
  expect(sent).toEqual([]);
  expect(s.lines()[0]).toMatch(/didn't catch that/);
});

test('on a phone without a recogniser there is no Speak button, and typing still works', () => {
  const v = fakeVoice(undefined, false);
  const s = screen(async () => reply([]), v.voice);
  expect(s.find('talk-speak')).toBeUndefined();
  expect(s.find('talk-input')).toBeDefined();
});

test('leaving the screen stops IRA speaking and stops listening', async () => {
  const v = fakeVoice();
  const s = screen(async () => reply([]), v.voice);
  await renderer.act(async () => { s.r.unmount(); });
  expect(v.events).toEqual(expect.arrayContaining(['stop-speaking', 'stop-listening']));
});
