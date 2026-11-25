import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Every colour pair the dashboard draws, checked in the stylesheet itself, in all four schemes:
 * light, dark, and each with "more contrast". Text needs 4.5:1 (WCAG AA); the focus ring, a
 * non-text indicator, 3:1. The stylesheet is the source, so a changed token is checked the day
 * it changes.
 */
const css = readFileSync(join(__dirname, '..', 'app', 'globals.css'), 'utf8');

function block(after: RegExp): Record<string, string> {
  // A scheme with no block of its own inherits its parent's colours, and is checked as such.
  const m = after.exec(css);
  if (!m) return {};
  const start = css.indexOf(':root {', m.index) + ':root {'.length;
  const body = css.slice(start, css.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)].map((x) => [x[1], x[2]]));
}

const light = block(/^:root \{/m);
const dark = { ...light, ...block(/@media \(prefers-color-scheme: dark\) \{/) };
const more = block(/@media \(prefers-contrast: more\) \{/);
const lightMore = { ...light, ...more };
// As the browser cascades it: the plain "more contrast" block comes after the dark one in the file,
// so it applies over dark too, then any block for exactly dark with more contrast.
const darkMore = { ...dark, ...more, ...block(/@media \(prefers-contrast: more\) and \(prefers-color-scheme: dark\) \{/) };

const lum = (hex: string) => {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a: string, b: string) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const WHITE = '#ffffff';

describe.each([['light', light], ['dark', dark], ['light, more contrast', lightMore], ['dark, more contrast', darkMore]])('%s', (_name, t) => {
  test('text and muted text are readable on the page and on cards', () => {
    for (const fg of ['text', 'muted']) for (const bg of ['bg', 'card']) expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });

  test('error text and notice-coloured text are readable', () => {
    for (const fg of ['sos-text', 'notice-text']) for (const bg of ['bg', 'card']) expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });

  test('white text is readable on every filled button and pill', () => {
    for (const fill of ['sos', 'urgent', 'notice', 'ok', 'fill-muted']) expect(ratio(WHITE, t[fill])).toBeGreaterThanOrEqual(4.5);
  });

  test('the skip link and the focus ring can be seen', () => {
    expect(ratio(t.bg, t.text)).toBeGreaterThanOrEqual(4.5);
    for (const bg of ['bg', 'card']) expect(ratio(t.focus, t[bg])).toBeGreaterThanOrEqual(3);
  });
});

test('each rule draws with the token meant for it', () => {
  expect(css).toMatch(/\.error \{ color: var\(--sos-text\); \}/);
  expect(css).toMatch(/button\.grey \{ background: var\(--fill-muted\); \}/);
  expect(css).toMatch(/\.pill\.grey \{ background: var\(--fill-muted\); \}/);
  expect(css).toMatch(/\.skip-link \{[^}]*background: var\(--text\); color: var\(--bg\);/);
  // A fill token is never used as a text colour.
  expect(css).not.toMatch(/[^-]color: var\(--(sos|notice|ok|urgent|fill-muted)\)/);
});
