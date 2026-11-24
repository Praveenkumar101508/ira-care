import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

/** No developer-facing words on any phone screen (phase 12): see the dashboard's twin test. */
const FORBIDDEN: { re: RegExp; why: string }[] = [
  { re: /\bAPI\b/, why: 'an API is not a thing a person sees' },
  { re: /\btoken\b/i, why: 'tokens are developer words' },
  { re: /\btier \d\b|\btier \{/, why: 'say who is called, not "tier N"' },
  { re: /\bJSON\b/, why: 'no JSON on a screen' },
  { re: /\binject/i, why: 'nothing is injected in the product' },
  { re: /\bsimulator\b/i, why: 'no simulator in the product' },
  { re: /\bDemo Person\b/, why: 'the demonstration people have names' },
  { re: /\buuid\b|\bID\b/, why: 'raw identifiers are not shown' },
  { re: /localhost|127\.0\.0\.1|10\.0\.2\.2|:8000\b/, why: 'no addresses on a screen' },
];
// Jest runs from the app's folder: the screens live under src/.
const ROOT = join(process.cwd(), 'src');
function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name: string) => {
    const p = join(dir, name);
    if (name === '__tests__') return [];
    return statSync(p).isDirectory() ? files(p) : /\.tsx$/.test(name) ? [p] : [];
  });
}
function visibleStrings(source: string): string[] {
  const out: string[] = [];
  for (const m of source.matchAll(/>([^<>{}]+)</g)) out.push(m[1]);
  for (const m of source.matchAll(/(?:placeholder|label|accessibilityLabel|accessibilityHint|hint|title)=["']([^"']+)["']/g)) out.push(m[1]);
  for (const m of source.matchAll(/(?:label|placeholder|hint)=\{['"]([^'"]+)['"]\}/g)) out.push(m[1]);
  return out.map((s) => s.trim()).filter((s) => s.length > 1);
}
test('no phone screen shows a developer word', () => {
  const offences: string[] = [];
  for (const f of files(join(ROOT, 'screens')).concat(files(join(ROOT, 'shell')))) {
    const src = readFileSync(f, 'utf8');
    for (const s of visibleStrings(src)) for (const rule of FORBIDDEN) if (rule.re.test(s)) offences.push(`${f.replace(ROOT, 'src')}: "${s}" (${rule.why})`);
  }
  expect(offences).toEqual([]);
});
