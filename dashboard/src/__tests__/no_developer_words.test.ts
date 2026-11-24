import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * No developer-facing words on any screen (phase 12). Every string a person
 * can read in the dashboard's pages and components is checked for the words
 * a product must not show: addresses and keys, raw identifiers, "tier N",
 * JSON, "inject", "simulator", "Demo Person". Test files and the presenter's
 * page (never shown to users or in recordings) are outside the rule.
 */
const FORBIDDEN: { re: RegExp; why: string }[] = [
  { re: /\bAPI\b/, why: 'an API is not a thing a carer sees' },
  { re: /\btoken\b/i, why: 'tokens are developer words' },
  { re: /\btier \d\b|\btier \{/, why: 'say who is called, not "tier N"' },
  { re: /\bJSON\b/, why: 'no JSON on a screen' },
  { re: /\binject/i, why: 'the presenter injects; the product does not say so' },
  { re: /\bsimulator\b/i, why: 'the browser watch is a watch, not a simulator' },
  { re: /\bDemo Person\b/, why: 'the demonstration people have names' },
  { re: /\buuid\b|\bID\b/, why: 'raw identifiers are not shown' },
  { re: /localhost|127\.0\.0\.1|:8000\b/, why: 'no addresses on a screen' },
];

const ROOT = join(__dirname, '..');
const EXCLUDED = ['__tests__', 'presenter'];

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (EXCLUDED.includes(name)) return [];
    return statSync(p).isDirectory() ? files(p) : /\.(tsx|ts)$/.test(name) ? [p] : [];
  });
}

/** The strings a person could read: JSX text and string literals in JSX attributes shown as text. */
function visibleStrings(source: string): string[] {
  const out: string[] = [];
  // text between tags, e.g. >Sign in<
  for (const m of source.matchAll(/>([^<>{}]+)</g)) out.push(m[1]);
  // template or string pieces inside JSX expressions that are plain text: `...` with no code, and '...' labels
  for (const m of source.matchAll(/(?:placeholder|aria-label|title|label|accessibilityLabel|accessibilityHint|alt)=["']([^"']+)["']/g)) out.push(m[1]);
  for (const m of source.matchAll(/\{'([^']{3,})'\}|\{"([^"]{3,})"\}/g)) out.push(m[1] ?? m[2]);
  return out.map((s) => s.trim()).filter((s) => s.length > 1 && !/^[\s\d.,:;()·%-]+$/.test(s));
}

test('no screen shows a developer word', () => {
  const offences: string[] = [];
  for (const f of files(join(ROOT, 'app')).concat(files(join(ROOT, 'components')))) {
    const src = readFileSync(f, 'utf8');
    for (const s of visibleStrings(src)) {
      for (const rule of FORBIDDEN) if (rule.re.test(s)) offences.push(`${f.replace(ROOT, 'src')}: "${s}" (${rule.why})`);
    }
  }
  expect(offences).toEqual([]);
});
