import React from 'react';
import renderer from 'react-test-renderer';
import { TextInput } from 'react-native';
import { JoinScreen } from '../screens/JoinScreen';
import { LoginScreen } from '../screens/LoginScreen';

/** iOS autocorrect rewrote a typed username (the simulator journey, run 46090062926, signed in as
 *  "sravani@exmple.coma" and was refused): no account field may be autocorrected or spell-checked. */
const fields = (el: React.ReactElement) => renderer.create(el).root.findAllByType(TextInput);

test('the sign-in fields are never autocorrected', () => {
  const r = renderer.create(<LoginScreen defaultBaseUrl="http://127.0.0.1:8000" onLoggedIn={() => {}} onJoin={() => {}} notice="" />);
  const advanced = r.root.findAll((n) => n.props.testID === 'advanced' && typeof n.props.onPress === 'function')[0];
  renderer.act(() => { advanced.props.onPress(); });
  const inputs = r.root.findAllByType(TextInput);
  expect(inputs.map((i) => i.props.testID)).toEqual(['username', 'password', 'server']);
  for (const i of inputs) expect(i.props.autoCorrect).toBe(false);
  const byId = Object.fromEntries(inputs.map((i) => [i.props.testID, i.props]));
  expect(byId.username.autoCapitalize).toBe('none');
  expect(byId.username.spellCheck).toBe(false);
  expect(byId.server.spellCheck).toBe(false);
});

test('the invitation fields are never autocorrected', () => {
  const inputs = fields(<JoinScreen baseUrl="http://127.0.0.1:8000" onBack={() => {}} onJoined={() => {}} />);
  expect(inputs.length).toBe(3);
  for (const i of inputs) expect(i.props.autoCorrect).toBe(false);
});
