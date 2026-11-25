import React from 'react';
import renderer from 'react-test-renderer';
import { TalkScreen } from '../screens/TalkScreen';
import type { Person, TalkReply } from '../api/types';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null };
const all = (n: Node | Node[] | string | null, out: Node[] = []): Node[] => {
  if (Array.isArray(n)) { n.forEach((c) => all(c, out)); return out; }
  if (n && typeof n !== 'string') { out.push(n); (n.children ?? []).forEach((c) => all(c, out)); }
  return out;
};
const text = (n: Node): string => (n.children ?? []).map((c) => (typeof c === 'string' ? c : text(c))).join('');

const person = { id: 'p1', display_name: 'Ada Lovelace', preferred_name: 'Ada' } as unknown as Person;

test('IRA\'s first sentence is on screen while the rest of the answer is still coming', async () => {
  let finish: (r: TalkReply) => void = () => {};
  const onSend = (_t: string, onSentence: (s: string) => void) => new Promise<TalkReply>((resolve) => {
    onSentence('Good morning, Ada.');
    finish = resolve;
  });
  const r = renderer.create(<TalkScreen person={person} onSend={onSend} onBack={() => {}} />);
  const find = (id: string) => all(r.toJSON() as Node).find((n) => n.props.testID === id);
  await renderer.act(async () => { (find('talk-input')!.props.onChangeText as (t: string) => void)('hello'); });
  await renderer.act(async () => { (find('talk-input')!.props.onSubmitEditing as () => void)(); });

  // The reply has not finished, yet the first sentence already shows.
  const shown = all(r.toJSON() as Node).filter((n) => String(n.props.testID ?? '').startsWith('talk-ira-')).map(text);
  expect(shown).toEqual(['IRA: Good morning, Ada.']);

  await renderer.act(async () => { finish({ sentences: ['Good morning, Ada.'], action: null, needsHelp: false }); });
  const after = all(r.toJSON() as Node).filter((n) => String(n.props.testID ?? '').startsWith('talk-ira-')).map(text);
  expect(after).toEqual(['IRA: Good morning, Ada.']); // shown once, not again at the end
});
