/**
 * Reading a streamed reply line by line as it arrives (remediation C-12).
 *
 * The server sends one JSON object per line (NDJSON). React Native's built-in fetch hands over
 * the body only when it is complete, so the person waited for the whole reply before seeing a
 * word. `expo/fetch` (Expo SDK 52) gives a readable body; this module reads it chunk by chunk and
 * hands on each complete line. Lines are split on the newline BYTE: in UTF-8, 0x0A never occurs
 * inside a multi-byte character, so a chunk boundary in the middle of "é" or an emoji cannot
 * break a line. Where no readable body exists (an older runtime, the web, a test double), the
 * whole text is read and split the same way, which is exactly the old behaviour.
 */

type Fetch = typeof fetch;

/** `expo/fetch` where it exists; null otherwise (jest, the web, an older Expo). */
export function streamingFetch(): Fetch | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require('expo/fetch') as { fetch?: Fetch };
    return typeof mod?.fetch === 'function' ? mod.fetch : null;
  } catch {
    return null;
  }
}

/** Decode UTF-8 bytes. TextDecoder where the runtime has one; a small decoder otherwise. */
export function utf8(bytes: Uint8Array): string {
  const TD = (globalThis as { TextDecoder?: new (label?: string) => { decode(b: Uint8Array): string } }).TextDecoder;
  if (TD) return new TD('utf-8').decode(bytes);
  let out = '';
  for (let i = 0; i < bytes.length;) {
    const b = bytes[i];
    let cp: number; let n: number;
    if (b < 0x80) { cp = b; n = 1; } else if (b >= 0xf0) { cp = b & 0x07; n = 4; } else if (b >= 0xe0) { cp = b & 0x0f; n = 3; } else { cp = b & 0x1f; n = 2; }
    for (let k = 1; k < n; k++) cp = (cp << 6) | ((bytes[i + k] ?? 0x80) & 0x3f);
    out += String.fromCodePoint(cp);
    i += n;
  }
  return out;
}

/** Call `onLine` for each non-empty line of the body, in order, as soon as it is complete. */
export async function forEachLine(response: Response, onLine: (line: string) => void): Promise<void> {
  const reader = (response.body as ReadableStream<Uint8Array> | null | undefined)?.getReader?.();
  if (!reader) {
    for (const line of (await response.text()).split('\n')) if (line.trim()) onLine(line);
    return;
  }
  let pending = new Uint8Array(0);
  for (;;) {
    const { value, done } = await reader.read();
    if (value && value.length) {
      const joined = new Uint8Array(pending.length + value.length);
      joined.set(pending); joined.set(value, pending.length);
      let start = 0;
      for (let i = 0; i < joined.length; i++) {
        if (joined[i] === 0x0a) {
          const line = utf8(joined.subarray(start, i));
          if (line.trim()) onLine(line);
          start = i + 1;
        }
      }
      pending = joined.slice(start);
    }
    if (done) break;
  }
  const last = utf8(pending);
  if (last.trim()) onLine(last);
}
