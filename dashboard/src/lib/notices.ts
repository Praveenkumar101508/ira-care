import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * The dashboard's third-party notices: its own copy of docs/legal/notices/dashboard-image.txt,
 * written and checked by `scripts/supply_chain.py render`. next.config.ts traces it into the
 * standalone output, so the production image serves the same file it ships in
 * /usr/share/doc/ira-care/THIRD_PARTY_NOTICES.txt.
 */
export const NOTICES_FILE = path.join('src', 'legal', 'THIRD_PARTY_NOTICES.txt');

/** The first line every notices file starts with (scripts/supply_chain.py notices_text). */
const HEADING = 'THIRD-PARTY NOTICES: ';

/**
 * Read the notices. Server side only. A missing or foreign file is an error, never an empty
 * page that looks as if nothing needed attributing.
 */
export function readNotices(file: string = path.join(process.cwd(), NOTICES_FILE)): string {
  const text = readFileSync(file, 'utf-8');
  if (!text.startsWith(HEADING)) throw new Error(`${file} is not a third-party notices file`);
  return text;
}

/** The artefact the notices describe: their first line, without the heading. */
export function noticesTitle(text: string): string {
  return text.slice(HEADING.length, text.indexOf('\n')).trim();
}
