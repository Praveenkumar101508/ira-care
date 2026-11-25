import { readFileSync } from 'fs';
import { join } from 'path';
import React from 'react';
import renderer, { act } from 'react-test-renderer';
import { ThemeProvider } from '../appearance/ThemeContext';
import { DEFAULT_APPEARANCE } from '../appearance/theme';
import { LicencesView, NOTICES, noticeParagraphs, type BundledNotices } from '../screens/LicencesScreen';
import { SettingsTab } from '../shell/SettingsTab';

/**
 * The family app carries its third-party notices and shows them (Settings, Open-source licences).
 * The bundled copy is docs/legal/notices/phone-app.txt as `scripts/supply_chain.py render` writes
 * it; every runtime dependency the app ships is named in it at the version the lock installs.
 */
const MOBILE = join(__dirname, '..', '..');
const source = readFileSync(join(MOBILE, '..', 'docs', 'legal', 'notices', 'phone-app.txt'), 'utf-8');
const pkg = JSON.parse(readFileSync(join(MOBILE, 'package.json'), 'utf-8')) as { dependencies: Record<string, string> };
const lock = JSON.parse(readFileSync(join(MOBILE, 'package-lock.json'), 'utf-8')) as { packages: Record<string, { version: string }> };

/** The app's direct runtime dependencies that the notices do not name at their locked version. */
function unlisted(text: string): string[] {
  return Object.keys(pkg.dependencies).filter((name) => !text.includes(`\n${name} ${lock.packages[`node_modules/${name}`].version}\n`));
}

const press = (r: renderer.ReactTestRenderer, id: string) => {
  const inst = r.root.findAll((n) => n.props.testID === id && typeof n.props.onPress === 'function')[0];
  act(() => { (inst.props.onPress as () => void)(); });
};
const settings = () => (
  <ThemeProvider appearance={DEFAULT_APPEARANCE}>
    <SettingsTab appearance={DEFAULT_APPEARANCE} onAppearance={() => {}} experience="family" circle={[]} consents={null} pushRegistered={false}
      onRegisterPush={() => {}} username="u@example.org" server="https://care.example.org" onSignOut={() => {}} onPairWatch={() => {}} onOpenCare={() => {}} onPickPhoto={async () => null} />
  </ThemeProvider>
);

test('the bundled notices are the phone app notices file, unedited', () => {
  expect(NOTICES.artefact).toBe('phone-app');
  expect(NOTICES.source).toBe('docs/legal/notices/phone-app.txt');
  expect(NOTICES.text).toBe(source);
  expect(NOTICES.text.startsWith('THIRD-PARTY NOTICES: IRA CARE family app')).toBe(true);
});

test('every runtime dependency of the app is named in them at its locked version', () => {
  expect(Object.keys(pkg.dependencies).length).toBeGreaterThan(10);
  expect(unlisted(NOTICES.text)).toEqual([]);
  // Negative control: notices missing one package are caught.
  const v = lock.packages['node_modules/react-native'].version;
  expect(unlisted(NOTICES.text.replace(`\nreact-native ${v}\n`, '\n'))).toEqual(['react-native']);
});

test('settings opens the licences screen, which shows every paragraph of the notices', () => {
  const r = renderer.create(settings());
  press(r, 'settings-licences');
  const list = r.root.find((n) => n.props.testID === 'licences' && Array.isArray(n.props.data));
  expect(list.props.data).toEqual(noticeParagraphs(NOTICES.text));
  expect(list.props.accessibilityLabel).toBe('Third-party notices');
  const shown = (list.props.data as string[]).join('\n\n');
  expect(unlisted(`\n${shown}\n`)).toEqual([]);
  expect(shown).toContain('----- MIT -----');
  // The first paragraphs are on screen as text, the heading among them.
  const texts = r.root.findAll((n) => n.props.selectable === true).map((n) => String(n.props.children));
  expect(texts[0]).toContain('THIRD-PARTY NOTICES: IRA CARE family app');
  const header = r.root.findAll((n) => n.props.accessibilityRole === 'header');
  expect(header.map((h) => h.props.children)).toContain('Open-source licences');
  press(r, 'licences-back');
  expect(r.root.findAll((n) => n.props.testID === 'settings-licences').length).toBeGreaterThan(0);
});

test('negative control: a screen given other notices shows those, so the check above is real', () => {
  const other: BundledNotices = { artefact: 'phone-app', source: 'x', text: 'THIRD-PARTY NOTICES: something else\n\nleft-pad 1.0.0\n  Licence: WTFPL\n' };
  const r = renderer.create(<ThemeProvider appearance={DEFAULT_APPEARANCE}><LicencesView notices={other} onBack={() => {}} /></ThemeProvider>);
  const list = r.root.find((n) => n.props.testID === 'licences' && Array.isArray(n.props.data));
  expect(list.props.data).toHaveLength(2);
  expect(unlisted(`\n${(list.props.data as string[]).join('\n\n')}\n`)).toContain('react-native');
});
