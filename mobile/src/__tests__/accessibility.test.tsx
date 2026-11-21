import React from 'react';
import renderer from 'react-test-renderer';
import { BigButton, Screen } from '../ui';
import { LoginScreen } from '../screens/LoginScreen';

type Node = { props: Record<string, unknown>; children?: (Node | string)[] | null };
const all = (n: Node | string | null, out: Node[] = []): Node[] => { if (n && typeof n !== 'string') { out.push(n); (n.children ?? []).forEach((c) => all(c, out)); } return out; };

test('big buttons announce their label, role and state', () => {
  const tree = renderer.create(<BigButton label="I'm on my way" onPress={() => {}} hint="Tells everyone you are going" disabled />).toJSON() as Node;
  const button = all(tree).find((n) => n.props.accessibilityRole === 'button');
  expect(button?.props.accessibilityLabel).toBe("I'm on my way");
  expect(button?.props.accessibilityHint).toBe('Tells everyone you are going');
  expect(button?.props.accessibilityState).toEqual({ disabled: true });
});

test('screens have a header and text scales with the system setting', () => {
  const tree = renderer.create(<Screen title="Alerts"><BigButton label="Back" onPress={() => {}} /></Screen>).toJSON() as Node;
  expect(all(tree).find((n) => n.props.accessibilityRole === 'header')).toBeTruthy();
  expect(all(tree).some((n) => n.props.maxFontSizeMultiplier !== undefined)).toBe(true);
});

test('every text field on the sign-in screen has an accessibility label', () => {
  const tree = renderer.create(<LoginScreen defaultBaseUrl="http://care.invalid" onLoggedIn={() => {}} />).toJSON() as Node;
  const inputs = all(tree).filter((n) => ['server', 'username', 'password'].includes(String(n.props.testID)));
  expect(inputs).toHaveLength(3);
  for (const i of inputs) expect(typeof i.props.accessibilityLabel).toBe('string');
});
